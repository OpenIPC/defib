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

# Recover a device via UART
defib burn -c hi3516ev300 -f u-boot.bin -p /dev/ttyUSB0

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
- macOS serial workaround (ACK byte correction)
- Cross-platform: Linux, macOS, Windows

## License

MIT
