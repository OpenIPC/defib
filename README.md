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

## Features

- All 3 HiSilicon/Goke UART protocols (Standard, V500, CV6xx)
- 120+ supported SoC chips
- Plugin architecture for future vendor protocols
- Multiple interfaces: CLI, TUI, Web UI, JSON for automation
- Network recovery (async TFTP server, broadcast discovery)
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
