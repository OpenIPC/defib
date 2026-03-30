# defib

Universal camera recovery tool - shocking dead devices back to life.

A modern, async Python tool for recovering bricked IP cameras via UART serial
and network protocols. Supports HiSilicon, Goke, and other SoC families.

## Installation

```bash
uv tool install defib
# or
pipx install defib
```

## Quick Start

```bash
# List supported chips
defib list-chips

# List available serial ports
defib ports

# Recover a device via UART
defib burn -c hi3516ev300 -f u-boot.bin -p /dev/ttyUSB0

# JSON output for automation
defib burn -c gk7205v200 -f u-boot.bin --output json
```

## License

MIT
