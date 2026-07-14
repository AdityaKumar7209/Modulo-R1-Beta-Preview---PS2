#!/usr/bin/env python3
"""
UDPFS Game Server - GUI

Cross-platform (Windows/macOS/Linux) GUI for serving PS2 game ISOs to
neutrino over UDPFS. Select one or more folders containing ISO files; each
folder appears as a top-level directory on the PS2's network browser.

Usage:
    python3 udpfs_gui_server.py                     # GUI (needs tkinter)
    python3 udpfs_gui_server.py --headless DIR...   # console mode, no GUI

The server listens on UDP port 0xF5F6 (62966). Make sure your firewall
allows inbound UDP on that port. Shares are served read-only.
"""

import argparse
import errno
import json
import os
import queue
import select
import socket
import struct
import sys
import threading
import time

import udpfs_server
from udpfs_server import (
    UdpfsServer, UDPFS_PORT, UDPRDMA_SVC_UDPFS, FIO_S_IFDIR,
    PacketType, Header, DiscHeader, DataHeader, DataFlags,
)


class TransferAbandoned(Exception):
    """Client re-DISCOVERed mid-transfer: drop the rest of this reply."""

# A game-busy IOP can starve the console's network thread for whole seconds;
# the upstream default gives up on a transfer after 0.4 s (4 x 0.1 s) without
# window progress, killing the session mid-game. Keep retransmitting for
# ~20 s instead - the PS2 client NACKs periodically and picks the transfer
# back up as soon as the console breathes again.
udpfs_server.MAX_WINDOW_RETRIES = 200

CONFIG_FILE = os.path.join(os.path.expanduser('~'), '.udpfs_gui_server.json')

# Sentinel returned by _resolve_path for the synthetic root directory that
# contains one entry per selected share.
VIRTUAL_ROOT = '\0virtual-root\0'


class FakeDirEntry:
    """Minimal os.DirEntry stand-in for share folders in the virtual root."""

    def __init__(self, name, path):
        self.name = name
        self.path = path

    def stat(self, follow_symlinks=True):
        return os.stat(self.path)


class MultiRootUdpfsServer(UdpfsServer):
    """UDPFS server presenting multiple share folders under one virtual root."""

    def __init__(self, shares, on_log=None, on_status=None, **kwargs):
        # shares: list of directory paths; displayed by basename (deduplicated)
        kwargs.setdefault('read_only', True)
        super().__init__(root_dir=None, block_device=None, **kwargs)

        self.shares = {}
        for path in shares:
            real = os.path.realpath(path)
            if not os.path.isdir(real):
                continue
            name = os.path.basename(real.rstrip('/\\')) or real
            candidate = name
            n = 2
            while candidate in self.shares:
                candidate = f"{name} ({n})"
                n += 1
            self.shares[candidate] = real

        # Advertise the share names in discovery so the PS2 loader's server
        # picker can show them next to this server.
        self.share_names = list(self.shares.keys())

        self.on_log = on_log
        self.on_status = on_status
        self.stop_event = threading.Event()

        # Second socket owning 127.0.0.1:port. A PS2 running in PCSX2 on this
        # same machine can only exchange packets with us via loopback: PCSX2's
        # own port-62966 socket swallows anything addressed to the LAN IP. By
        # answering local clients from this socket, the PS2 locks onto
        # 127.0.0.1 and every request lands here.
        self.lo_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.lo_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.lo_sock.bind(('127.0.0.1', self.port))
        except OSError:
            self.lo_sock.close()
            self.lo_sock = None
        self._local_addr_cache = {}

    # --- path mapping ---

    def _resolve_path(self, client_path):
        while client_path.startswith('/') or client_path.startswith('\\'):
            client_path = client_path[1:]
        client_path = client_path.replace('\\', '/').rstrip('/')

        if client_path == '':
            return VIRTUAL_ROOT

        parts = client_path.split('/', 1)
        share_root = self.shares.get(parts[0])
        if share_root is None:
            return None

        if len(parts) == 1:
            return share_root

        resolved = os.path.realpath(os.path.join(share_root, parts[1]))
        if resolved != share_root and not resolved.startswith(share_root + os.sep):
            return None
        return resolved

    def _virtual_root_stat(self):
        return {
            'mode': FIO_S_IFDIR,
            'attr': 0,
            'size': 0,
            'hisize': 0,
            'ctime': b'\x00' * 8,
            'atime': b'\x00' * 8,
            'mtime': b'\x00' * 8,
        }

    # --- request overrides for the virtual root ---

    def _handle_open(self, addr, payload):
        if len(payload) >= 8:
            _, is_dir, _, _ = struct.unpack('<BBHi', payload[:8])
            path = payload[8:].split(b'\x00')[0].decode('utf-8', errors='replace')
            if is_dir and self._resolve_path(path) == VIRTUAL_ROOT:
                self.stats['open'] += 1
                entries = [FakeDirEntry(name, real)
                           for name, real in sorted(self.shares.items())]
                handle = self._alloc_handle({'entries': entries, 'index': 0},
                                            is_dir=True)
                if handle < 0:
                    self._send_open_reply(addr, handle)
                    return
                self._print_event(f"[{addr[0]}:{addr[1]}] DOPEN '/' -> "
                                  f"{len(entries)} shares, handle={handle}")
                self._send_open_reply(addr, handle,
                                      stat_info=self._virtual_root_stat())
                return
        super()._handle_open(addr, payload)

    def _handle_getstat(self, addr, payload):
        if len(payload) >= 4:
            path = payload[4:].split(b'\x00')[0].decode('utf-8', errors='replace')
            if self._resolve_path(path) == VIRTUAL_ROOT:
                self.stats['getstat'] += 1
                self._send_getstat_reply(addr, result=0,
                                         stat_info=self._virtual_root_stat())
                return
        super()._handle_getstat(addr, payload)

    def _handle_discovery(self, data, addr, hdr):
        # The PS2 sends each discovery attempt both as broadcast and as a
        # unicast probe, with the same sequence number. Answer the second
        # copy by repeating the previous INFORM instead of consuming a new
        # sequence number, so neither side desyncs. Only a copy arriving
        # right after the first is that duplicate: a fresh session (IOP
        # reboot) may reuse the same starting sequence number and must get
        # the full state reset instead.
        now = time.monotonic()
        last = getattr(self, '_last_disc', None)
        if (last is not None and last[0] == (addr, hdr.seq_nr)
                and now - last[1] < 2.0):
            pkt = (Header(packet_type=PacketType.INFORM,
                          seq_nr=(self.tx_seq_nr - 1) & 0xFFF).pack()
                   + DiscHeader(service_id=UDPRDMA_SVC_UDPFS).pack())
            self.sock.sendto(pkt, addr)
            return
        self._last_disc = ((addr, hdr.seq_nr), now)
        super()._handle_discovery(data, addr, hdr)

    def _wait_for_window_ack(self, addr):
        # Same as upstream, plus: a DISCOVERY arriving mid-transfer means the
        # client timed out on this reply and is re-syncing its session. The
        # upstream loop silently drops it, leaving the client unable to ever
        # reconnect while we keep retransmitting. Process it and abandon the
        # transfer - pushing the remaining stale chunks into the fresh
        # session would corrupt the next reply.
        self.sock.settimeout(udpfs_server.WINDOW_ACK_TIMEOUT)
        try:
            while True:
                pkt, recv_addr = self.sock.recvfrom(2048)
                if len(pkt) < 6:
                    continue
                hdr = Header.unpack(pkt)
                if hdr.packet_type == PacketType.DISCOVERY:
                    self.sock.settimeout(None)
                    self._handle_discovery(pkt, recv_addr, hdr)
                    self.tx_buffer = []
                    raise TransferAbandoned()
                if hdr.packet_type != PacketType.DATA:
                    continue
                data_hdr = DataHeader.unpack(pkt[2:6])
                if data_hdr.data_byte_count > 0 or data_hdr.hdr_word_count > 0:
                    # A new REQUEST mid-transfer: the client gave up on this
                    # reply and moved on. Swallowing it would deadlock the
                    # session (we retransmit stale chunks it will never
                    # accept, it waits for a reply we never produce). Drop
                    # the stale transfer; run() re-dispatches the request.
                    self.tx_buffer = []
                    self._pending_pkt = (pkt, recv_addr)
                    raise TransferAbandoned()
                if data_hdr.flags & DataFlags.ACK:
                    self.tx_seq_nr_acked = data_hdr.seq_nr_ack
                    self.tx_buffer = [
                        (seq, p) for seq, p in self.tx_buffer
                        if ((seq - data_hdr.seq_nr_ack - 1) & 0xFFF) < 2048
                    ]
                    return
                else:
                    self.tx_seq_nr_acked = (data_hdr.seq_nr_ack - 1) & 0xFFF
                    self._retransmit_from(addr, data_hdr.seq_nr_ack)
                    return
        except socket.timeout:
            self.sock.settimeout(None)
            if self.tx_buffer:
                self._retransmit_from(addr, self.tx_buffer[0][0])
            return
        finally:
            self.sock.settimeout(None)

    def _retransmit_from(self, addr, from_seq):
        # Re-blasting the whole unacked window back-to-back re-overruns the
        # PS2's 16 KB RX FIFO - the very condition that lost the packets in
        # the first place. Pace retransmits instead; they only happen after
        # loss, so normal throughput is unaffected.
        #
        # Rate-limit repeats: a client streaming NACKs (one per duplicate it
        # receives) must not multiply into a whole-window retransmit per
        # NACK, or the recovery traffic itself sustains the storm.
        now = time.monotonic()
        last = getattr(self, '_last_retx', None)
        if last is not None and last[0] == from_seq and now - last[1] < 0.05:
            return
        self._last_retx = (from_seq, now)
        count = 0
        for seq, packet in self.tx_buffer:
            if ((seq - from_seq) & 0xFFF) < 2048:
                if count:
                    time.sleep(0.002)
                self.sock.sendto(packet, addr)
                count += 1
        if self.verbose and count > 0:
            self._print_event(
                f"  Retransmitted {count} packets from seq={from_seq} (paced)")

    def _send_raw_data_with_header(self, addr, header, data):
        if self.lo_sock is None or self.sock is not self.lo_sock:
            super()._send_raw_data_with_header(addr, header, data)
            return

        # PCSX2's SMAP emulation corrupts the combined header+data packet
        # (the data lands on a non-16-byte-aligned FIFO read). For local
        # clients, ship the app header in its own packet, data separately.
        self.tx_buffer = []
        self.tx_start_seq = self.tx_seq_nr

        self._send_data_packet(addr, header, fin=(len(data) == 0),
                               hdr_size=len(header))

        max_chunk = self._optimal_chunk_size(len(data))
        offset = 0
        window_retries = 0
        while offset < len(data):
            if self._in_flight() >= 8:  # SEND_WINDOW
                old_acked = self.tx_seq_nr_acked
                self._wait_for_window_ack(addr)
                if self.tx_seq_nr_acked == old_acked:
                    window_retries += 1
                    if window_retries >= udpfs_server.MAX_WINDOW_RETRIES:
                        self._print_event("  Window ACK retries exhausted, aborting transfer")
                        return
                else:
                    window_retries = 0
                continue

            window_retries = 0
            chunk_size = min(max_chunk, len(data) - offset)
            is_last = (offset + chunk_size >= len(data))
            self._send_data_packet(addr, data[offset:offset + chunk_size],
                                   fin=is_last)
            offset += chunk_size

    # --- output hooks ---

    def _print_event(self, msg):
        msg = f"{time.strftime('%H:%M:%S')} {msg}"
        if self.on_log is not None:
            self.on_log(msg)
        else:
            super()._print_event(msg)

    def _update_status(self):
        if self.on_status is None:
            super()._update_status()
            return
        now = time.monotonic()
        if now - self._last_status_time < 1.0:
            return
        dt = now - self._last_status_time if self._last_status_time else 1.0
        total = self.stats['bytes_read'] + self.stats['bytes_written']
        rate = (total - self._last_status_bytes) / dt if dt > 0 else 0
        self.on_status(total, rate)
        self._last_status_time = now
        self._last_status_bytes = total

    # --- stoppable main loop ---

    def _addr_is_local(self, ip):
        """True if ip belongs to this machine (a bind test, cached)."""
        cached = self._local_addr_cache.get(ip)
        if cached is not None:
            return cached
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.bind((ip, 0))
            local = True
        except OSError:
            local = False
        finally:
            probe.close()
        self._local_addr_cache[ip] = local
        return local

    def run(self):
        names = ', '.join(self.shares.keys()) if self.shares else '(none)'
        self._print_event(f"Serving shares: {names}")
        self._print_event(f"Listening on UDP port {self.port} (0x{self.port:04X}), read-only")

        main_sock = self.sock
        socks = [main_sock] + ([self.lo_sock] if self.lo_sock else [])
        while not self.stop_event.is_set():
            try:
                ready, _, _ = select.select(socks, [], [], 0.5)
            except OSError:
                break
            for rx_sock in ready:
                try:
                    data, addr = rx_sock.recvfrom(2048)
                except OSError:
                    continue

                # Serve this exchange on the socket the client can reach:
                # loopback for same-machine clients (PCSX2), main otherwise.
                reply_sock = rx_sock
                if (rx_sock is main_sock and self.lo_sock is not None
                        and len(data) >= 2
                        and Header.unpack(data).packet_type == PacketType.DISCOVERY
                        and self._addr_is_local(addr[0])):
                    reply_sock = self.lo_sock
                    self._print_event(f"[{addr[0]}:{addr[1]}] local client, replying via 127.0.0.1")

                self.sock = reply_sock
                try:
                    while True:
                        try:
                            self._handle_packet(data, addr)
                        except TransferAbandoned:
                            self._print_event(
                                "  Transfer abandoned: client moved on")
                            pending = getattr(self, '_pending_pkt', None)
                            self._pending_pkt = None
                            if pending is not None:
                                # The packet that interrupted the transfer is
                                # a live request - serve it, don't drop it.
                                data, addr = pending
                                continue
                        break
                except Exception as e:  # keep serving on malformed input
                    self._print_event(f"ERROR handling packet: {type(e).__name__}: {e}")
                finally:
                    self.sock = main_sock

        self.sock = main_sock
        self._cleanup()
        main_sock.close()
        if self.lo_sock is not None:
            self.lo_sock.close()
        self._print_event("Server stopped.")

    def stop(self):
        self.stop_event.set()


def get_local_ips():
    """Best-effort list of LAN IPs, for display only."""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return ips


def load_settings():
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(settings):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings, f, indent=2)
    except OSError:
        pass


def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext

    class ServerGui:
        def __init__(self, root):
            self.root = root
            self.server = None
            self.server_thread = None
            self.log_queue = queue.Queue()
            self.status_queue = queue.Queue()

            root.title("UDPFS Game Server")
            root.minsize(560, 480)

            settings = load_settings()

            # Share folder list
            top = tk.LabelFrame(root, text="Game folders (served to the PS2)")
            top.pack(fill=tk.BOTH, expand=False, padx=8, pady=(8, 4))

            list_frame = tk.Frame(top)
            list_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
            self.folder_list = tk.Listbox(list_frame, height=6, selectmode=tk.EXTENDED)
            self.folder_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            scroll = tk.Scrollbar(list_frame, command=self.folder_list.yview)
            scroll.pack(side=tk.RIGHT, fill=tk.Y)
            self.folder_list.config(yscrollcommand=scroll.set)

            btns = tk.Frame(top)
            btns.pack(fill=tk.X, padx=4, pady=(0, 4))
            tk.Button(btns, text="Add Folder...", command=self.add_folder).pack(side=tk.LEFT)
            tk.Button(btns, text="Remove Selected", command=self.remove_selected).pack(side=tk.LEFT, padx=6)

            self.compression_var = tk.BooleanVar(value=settings.get('compression', False))
            tk.Checkbutton(btns, text="Serve .zso/.cso/.chd as .iso",
                           variable=self.compression_var).pack(side=tk.RIGHT)

            # Start/stop + status
            ctrl = tk.Frame(root)
            ctrl.pack(fill=tk.X, padx=8, pady=4)
            self.start_button = tk.Button(ctrl, text="Start Server", width=14,
                                          command=self.toggle_server)
            self.start_button.pack(side=tk.LEFT)
            self.status_var = tk.StringVar(value="Stopped")
            tk.Label(ctrl, textvariable=self.status_var, anchor='w').pack(
                side=tk.LEFT, fill=tk.X, expand=True, padx=8)

            # Log pane
            log_frame = tk.LabelFrame(root, text="Log")
            log_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 8))
            self.log_text = scrolledtext.ScrolledText(log_frame, height=12,
                                                      state=tk.DISABLED)
            self.log_text.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

            for folder in settings.get('folders', []):
                if os.path.isdir(folder):
                    self.folder_list.insert(tk.END, folder)

            root.protocol("WM_DELETE_WINDOW", self.on_close)
            root.after(100, self.poll_queues)

        # --- GUI actions ---

        def add_folder(self):
            folder = filedialog.askdirectory(title="Select a folder containing ISO files")
            if folder and folder not in self.folder_list.get(0, tk.END):
                self.folder_list.insert(tk.END, folder)
                self.persist()

        def remove_selected(self):
            for idx in reversed(self.folder_list.curselection()):
                self.folder_list.delete(idx)
            self.persist()

        def persist(self):
            save_settings({
                'folders': list(self.folder_list.get(0, tk.END)),
                'compression': self.compression_var.get(),
            })

        def toggle_server(self):
            if self.server is None:
                self.start_server()
            else:
                self.stop_server()

        def start_server(self):
            folders = list(self.folder_list.get(0, tk.END))
            if not folders:
                messagebox.showwarning("UDPFS Server",
                                       "Add at least one folder containing ISO files.")
                return
            self.persist()
            try:
                self.server = MultiRootUdpfsServer(
                    folders,
                    on_log=self.log_queue.put,
                    on_status=lambda total, rate: self.status_queue.put((total, rate)),
                    enable_compression=self.compression_var.get(),
                )
            except OSError as e:
                messagebox.showerror("UDPFS Server", f"Could not start server:\n{e}")
                self.server = None
                return

            self.server_thread = threading.Thread(target=self.server.run, daemon=True)
            self.server_thread.start()

            ips = get_local_ips()
            where = f" — this PC: {', '.join(ips)}" if ips else ""
            self.status_var.set(f"Running on UDP port {UDPFS_PORT} (0x{UDPFS_PORT:04X}){where}")
            self.start_button.config(text="Stop Server")

        def stop_server(self):
            if self.server is not None:
                self.server.stop()
                self.server_thread.join(timeout=3)
                self.server = None
                self.server_thread = None
            self.status_var.set("Stopped")
            self.start_button.config(text="Start Server")

        # --- background -> GUI plumbing ---

        def poll_queues(self):
            try:
                while True:
                    msg = self.log_queue.get_nowait()
                    self.log_text.config(state=tk.NORMAL)
                    self.log_text.insert(tk.END, msg + "\n")
                    self.log_text.see(tk.END)
                    self.log_text.config(state=tk.DISABLED)
            except queue.Empty:
                pass

            try:
                total, rate = None, None
                while True:
                    total, rate = self.status_queue.get_nowait()
            except queue.Empty:
                pass
            if total is not None and self.server is not None:
                self.status_var.set(
                    f"Running — served {total / (1024 * 1024):.1f} MB "
                    f"@ {rate / (1024 * 1024):.1f} MB/s")

            self.root.after(100, self.poll_queues)

        def on_close(self):
            self.stop_server()
            self.persist()
            self.root.destroy()

    root = tk.Tk()
    ServerGui(root)
    root.mainloop()


def run_headless(folders, enable_compression=False, verbose=False):
    server = MultiRootUdpfsServer(
        folders,
        enable_compression=enable_compression,
        verbose=verbose,
    )
    if not server.shares:
        print("Error: no valid folders to serve")
        return 1
    try:
        server.run()
    except KeyboardInterrupt:
        server.stop()
    return 0


def main():
    parser = argparse.ArgumentParser(description="UDPFS Game Server (GUI)")
    parser.add_argument('--headless', nargs='+', metavar='FOLDER',
                        help='Run without GUI, serving the given folders')
    parser.add_argument('--enable-compression', '-c', action='store_true',
                        help='Serve .zso/.cso/.chd files as .iso')
    parser.add_argument('--verbose', '-v', action='store_true')
    args = parser.parse_args()

    if args.headless:
        sys.exit(run_headless(args.headless, args.enable_compression, args.verbose))

    try:
        run_gui()
    except ImportError:
        print("tkinter is not available. Install it (e.g. 'sudo apt install python3-tk')")
        print("or run in console mode:  python3 udpfs_gui_server.py --headless FOLDER...")
        sys.exit(1)


if __name__ == '__main__':
    main()
