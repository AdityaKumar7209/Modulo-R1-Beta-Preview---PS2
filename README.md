# Modulo — Beta Preview

> [!WARNING]
> This release is a **beta preview**. Features may be incomplete, and crashes, transfer interruptions, compatibility problems, or other bugs may occur.

Modulo is an independent PlayStation 2 homebrew application based on modified components from the Neutrino project.

The modified Python UDPFS server source is included in this repository. The Modulo PS2-side source code is currently undergoing cleanup and preparation and is planned for publication with a later release.

## Included Files

```text
/
├── README.md
├── neutrino.elf
├── Neutrino-AFL-3.0.txt
├── Oxanium-OFL.txt
└── udpfs_server/
    ├── udpfs_gui_server.py
    ├── udpfs_server.py
    └── compressed_iso/
```

## Game Storage and Loading

Modulo provides a hierarchical directory browser.

Game images can be organized in folders and subfolders instead of being limited to fixed `CD` or `DVD` directories.

### USB Storage (`mass:/`)

- **Filesystem:** FAT32 or exFAT
- **Recommended filesystem:** exFAT, because FAT32 has a 4 GiB maximum file-size limit

Copy your game images to the USB drive. They may be placed in the root directory or organized into folders.

Select the USB card on the Modulo dashboard, choose the appropriate device, browse to a game, and launch it.


### MX4SIO SD Adapters

> [!NOTE]
> MX4SIO is currently not supported, even though an MX4SIO option appear in the menu.


### MMCE Memory Card Emulators

- **Filesystem:** An SD card configured for PS2 game-loading mode by the MMCE device
- **Firmware:** Version 1.4.0 or newer
https://sd2psxtd.github.io/

Copy your game images to the MMCE SD card, insert the device into a PS2 memory card slot, and select the corresponding MMCE device in Modulo.

Firmware versions older than 1.4.0 are not guaranteed to work.


### Internal HDD

- **Filesystem:** exFAT
- **Partition table:** MBR or GPT

The internal PS2 HDD can be formatted with a standard exFAT partition on a computer. Copy your game images to the drive, reinstall it in the PS2, and browse the files directly in Modulo.


### Network Streaming (UDPFS)

- **Server applications:** [`udpfs_gui_server.py`](udpfs_server/udpfs_gui_server.py) and [`udpfs_server.py`](udpfs_server/udpfs_server.py)
- **Protocol:** UDPFS
- **Port:** UDP `62966` (`0xF5F6`)
- **Supported formats:** ISO, CHD, CSO, and ZSO, subject to the dependencies described below

Run the Python UDPFS server on a computer, select the folders containing your game images, and start the server.

On the PS2, select **Browse network games**. Modulo will scan the local network, list available servers, and allow you to browse and stream supported game images.

## UDPFS Game Server

The included cross-platform Python application serves PlayStation 2 game images to **Modulo or Neutrino** over a local network using UDPFS.

### Requirements

- Python 3
- A local network connection between the computer and PS2
- DHCP enabled, unless static IP addresses are configured manually
- Inbound UDP traffic permitted on port **62966** (`0xF5F6`)
- Tkinter for the graphical interface

Tkinter is commonly included with Python. On Debian or Ubuntu, it can be installed with:

```bash
sudo apt install python3-tk
```

## Quick Start — Graphical Interface

1. Download and extract the beta preview.
2. Open a terminal in the `udpfs_server` folder.
3. Start the application using the command for your operating system.

### Windows

```powershell
py udpfs_gui_server.py
```

### macOS or Linux

```bash
python3 udpfs_gui_server.py
```

4. Select **Add Folder...** and choose one or more folders containing your game images.
5. Enable **Serve .zso/.cso/.chd as .iso** when compressed-image support is required.
6. Select **Start Server**.
7. Allow Python through the firewall when prompted.
8. Start Modulo Beta Preview on the PS2 and connect to the UDPFS server.

## Supported Image Formats

| Format | Support | Additional requirement |
| --- | --- | --- |
| ISO | Built in | None |
| CSO | Optional compression mode | None; uses Python's built-in `zlib` |
| ZSO | Optional compression mode | Python `lz4` package |
| CHD v5 | Optional compression mode | `libchdr` shared library |

### ZSO Support

Install the Python `lz4` package with:

```bash
python3 -m pip install lz4
```

On Windows, use:

```powershell
py -m pip install lz4
```

### CHD Support

On Debian or Ubuntu, `libchdr` can usually be installed with:

```bash
sudo apt install libchdr0
```

When compression support is enabled, supported compressed images are presented to the PS2 as ISO files.

## Network Configuration

> [!CAUTION]
> The server listens for UDP traffic on port **62966**. Only run the server on a trusted local network. Do not expose it directly to the public internet.

For reliable operation:

- Connect the PS2 and computer to the same local network.
- Use wired Ethernet wherever possible.
- Avoid using Wi-Fi for the server computer when consistent transfer performance is required.
- Permit inbound UDP traffic for Python on private networks.
- Ensure UDP port `62966` is not blocked by the operating-system firewall, router isolation, VPN, or security software.
- Avoid running multiple UDPFS servers on the same address and port.

## Troubleshooting

### The PS2 Cannot Find the Server

- Confirm that the application reports the server status as **Running**.
- Confirm that the PS2 and computer are connected to the same local network.
- Allow inbound UDP traffic on port `62966` through the computer's firewall.
- Temporarily disable any VPN or virtual network adapter that may be changing network routing.
- Restart the UDPFS server after changing network settings.
- Restart Modulo after confirming that the server is running.

## Beta Feedback

Bug reports should include:

- PS2 model and region
- Network adapter and network configuration
- Modulo beta version
- Game title and image format
- Whether real PS2 hardware or PCSX2 was used
- Steps required to reproduce the problem
- Relevant UDPFS server log output


## Source Availability

This beta includes:

- A prebuilt Modulo PS2-side executable
- Modified Neutrino components
- The modified Python UDPFS server source code

The complete Modulo PS2-side source code is undergoing cleanup and preparation. It is planned for publication with a later release.

## Credits

Modulo is based on the Neutrino project and its UDPFS ecosystem.

Neutrino was created by **Rick Gaiser (Maximus32)**, with additional work from the Neutrino contributors.

Neutrino upstream project:

<https://github.com/rickgaiser/neutrino>

Modulo is an independent project and is not an official Neutrino release.

## Third-Party Software Notice

Modulo includes modified software derived from Neutrino.

Neutrino is licensed under the **Academic Free License version 3.0 (AFL-3.0)**. A copy of the license is included in:

```text
Neutrino-AFL-3.0.txt
```

The original Neutrino copyright, attribution, and licensing notices remain applicable to the Neutrino-derived components.

The Oxanium font is distributed under the **SIL Open Font License version 1.1**. A copy of that license is included in:

```text
Oxanium-OFL.txt
```

The names of the Neutrino project, its creator, and its contributors are used for attribution only and do not imply endorsement of Modulo.

## Development Note

Modulo R1 was built with the help of vibe coding and AI-assisted development tools.

AI was used throughout development for coding, debugging, experimentation, and documentation. The project remains actively tested and refined by its author.

## Disclaimer

Modulo is an independent homebrew project. It is not affiliated with, endorsed by, or sponsored by Sony Interactive Entertainment.

PlayStation and PlayStation 2 are trademarks of their respective owners.


---

**Modulo by Aditya7286**
