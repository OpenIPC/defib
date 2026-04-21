# defib

Universal camera recovery tool - shocking dead devices back to life.

A modern, async Python tool for recovering bricked IP cameras via UART serial
and network protocols. Supports HiSilicon, Goke, and other SoC families.

## Web UI (No Install Needed)

**[Launch Web Recovery Tool](https://openipc.github.io/defib/)** — works directly
in Chrome/Edge/Opera using the WebSerial API. Just open the page, select your
chip and firmware, connect your USB-serial adapter, and go.

## Installation (CLI)

```bash
uv tool install defib
# or
pipx install defib
```

## Quick Start

```bash
# List supported chips (120+)
defib list-chips

# List available serial ports
defib ports

# Recover a device via UART using a raw device path
defib burn -c hi3516ev300 -f u-boot.bin -p /dev/ttyUSB0

# Recover a device via UART using a stable alias
defib burn -c hi3516ev300 -f u-boot.bin -p /dev/uart-orangepi5plus

# Interactive TUI
defib tui

# Network recovery via TFTP
defib network -f firmware.bin --nic eth0

# JSON output for AI agents / automation
defib burn -c gk7205v200 -f u-boot.bin --output json
```

## Full Firmware Install (UART + TFTP)

Flash a complete OpenIPC release (U-Boot + kernel + rootfs) in one command:

```bash
defib install -c hi3516ev300 \
  --firmware openipc.hi3516ev300-nor-lite.tgz \
  -p /dev/uart-IVG85HG50PYA-S \
  --power-cycle --nor-size 8
```

The install command orchestrates the entire process:
1. Extracts and verifies the firmware tarball (MD5 checksums)
2. Downloads U-Boot from OpenIPC (or uses cached copy)
3. Burns U-Boot to RAM via boot ROM protocol
4. Breaks into U-Boot console
5. Starts a multi-file TFTP server and configures U-Boot networking
6. Flashes each partition (U-Boot, kernel, rootfs) with CRC32 verification
7. Saves the boot environment and resets

Requires root for TFTP port 69 and NIC IP assignment. Supports both 8MB and
16MB NOR flash layouts (`--nor-size 8` or `--nor-size 16`).

## Features

- All 3 HiSilicon/Goke UART protocols (Standard, V500, CV6xx)
- 120+ supported SoC chips
- Full firmware install via UART + TFTP with CRC32 verification
- Plugin architecture for future vendor protocols
- Multiple interfaces: CLI, TUI, Web UI, JSON for automation
- Multi-file TFTP server with filename-based routing
- UART session capture/replay (.dcap format)
- Friendly serial-port discovery for multi-UART setups
- macOS serial workaround (ACK byte correction)
- Cross-platform: Linux, macOS, Windows

## Automated Power Cycling (PoE)

Defib can automatically power-cycle devices via a MikroTik PoE switch,
eliminating manual intervention for recovery loops and research workflows.

```bash
export DEFIB_POE_HOST=192.168.88.1
export DEFIB_POE_USER=admin
export DEFIB_POE_PASS=

defib burn -c hi3516ev300 -p /dev/uart-IVG85HG50PYA-S --power-cycle -b

# Burn and open serial terminal to see boot output (Ctrl-C to exit)
defib burn -c hi3516ev300 -p /dev/uart-IVG85HG50PYA-S --power-cycle -t
```

The `-t` flag auto-detects the post-boot mode:
- **Normal U-Boot shell** (e.g. hi3516ev300): raw terminal passthrough — type commands directly
- **Download command mode** (e.g. hi3516av200): interactive `defib>` prompt that wraps commands in HiSilicon's XHEAD/XCMD protocol, enabling flash operations on devices that enter `download_process()` after serial boot

```bash
# Interactive download command mode (hi3516av200 enters this automatically)
defib burn -c hi3516av200 -p /dev/ttyUSB0 --power-cycle -t
# defib> nand info
# Device 0: nand0, sector size 128 KiB
# [OK]
# defib> nand erase 0x200000 0x800000
# [OK]
```

The `--power-cycle` flag connects to the RouterOS API, auto-discovers the PoE
port by matching the serial device name against switch interface comments, and
power-cycles the device before recovery. A continuous ACK mechanism ensures the
bootrom is caught even on fast-booting devices where the bootrom window is <100ms.

Tested on real hardware with CRS112-8P-4S:

| Camera | SoC | Serial Port |
|--------|-----|-------------|
| IVGHP203Y-AF | hi3516cv300 | `/dev/uart-IVGHP203Y-AF` |
| IVG85HG50PYA-S | hi3516ev300 | `/dev/uart-IVG85HG50PYA-S` |

## Flash Agent (High-Speed Recovery)

Defib includes a bare-metal flash agent that runs directly on the SoC,
replacing U-Boot in the boot chain. It communicates over a COBS binary
protocol at 921600 baud for high-speed flash operations.

### One-Command Firmware Install

Flash a complete firmware image via UART in a single command:

```bash
defib agent flash -c hi3516ev300 -i firmware.bin -p /dev/ttyUSB0
```

Power-cycle the camera when prompted. The command handles everything:
1. Uploads the bare-metal agent via boot protocol
2. Switches to 921600 baud for high-speed transfer
3. Streams firmware directly to flash (skips 0xFF sectors)
4. Verifies CRC32 of the written data
5. Reboots the device

Typical 8MB OpenIPC firmware on 16MB flash: **~2 minutes** total (upload +
flash + verify + boot). No network required — just a USB-serial adapter.

### Other Agent Commands

```bash
# Upload agent only (for manual operations)
defib agent upload -c hi3516ev300 -p /dev/ttyUSB0

# Dump the entire flash (address and size auto-detected)
defib agent read -p /dev/ttyUSB0 -o flash_dump.bin

# Query device info (flash size, RAM base, JEDEC ID, agent version)
defib agent info -p /dev/ttyUSB0

# Write data back to flash
defib agent write -p /dev/ttyUSB0 -i flash_dump.bin

# Scan flash health (bad sectors, stuck bits)
defib agent scan -p /dev/ttyUSB0
```

Address defaults to flash base (`0x14000000`) and size is auto-detected
from the device. Override with `-a` and `-s` if needed. Use `--no-verify`
to skip the CRC32 check, or `--output json` for automation. See
[agent/README.md](agent/README.md) for protocol details and supported chips.

## Testing with QEMU

Defib can be tested end-to-end against the
[qemu-hisilicon](https://github.com/OpenIPC/qemu-hisilicon) emulator without
physical hardware. When QEMU starts without `-kernel`, it emulates the HiSilicon
boot ROM serial protocol.

```bash
# Terminal 1: start QEMU in fastboot mode
qemu-system-arm -M hi3516ev300 -m 64M -nographic \
    -chardev socket,id=ser0,path=/tmp/qemu-hisi.sock,server=on,wait=off \
    -serial chardev:ser0

# Terminal 2: recover via socket (auto-downloads OpenIPC U-Boot)
defib burn -c hi3516ev300 -p socket:///tmp/qemu-hisi.sock
```

The `socket://` transport prefix connects defib directly to QEMU's chardev Unix
socket. All three protocol phases (DDR init, SPL, U-Boot) transfer with full
CRC-16 validation. After the upload, QEMU starts the CPU and U-Boot output
appears on the same connection.

Supported: all Standard protocol chips (hi3516cv300, hi3516ev200, hi3516ev300,
hi3518ev300, gk7205v200, gk7205v300, etc.). V500 and CV6xx protocol emulation
is not yet available in QEMU.

## License

MIT
