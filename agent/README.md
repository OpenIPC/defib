# defib Flash Agent

Bare-metal ARM32 binary that runs directly on HiSilicon/Goke SoCs without
U-Boot or Linux. Provides fast binary flash read/write over UART using
the COBS protocol — ~5x faster than U-Boot's `md.b` hex dump.

## Boot Sequence

The HiSilicon boot ROM loads code in three stages:

```
Stage 1: DDR Step (64 bytes)     → Loaded to SRAM, initializes DDR
Stage 2: SPL     (24 KB)         → Loaded to SRAM, initializes clocks/pins/DDR tuning
Stage 3: U-Boot  (any size)      → Loaded to DDR, CPU jumps here
```

**The agent replaces only Stage 3 (U-Boot).** Stages 1 and 2 MUST contain
real initialization code — the DDR step comes from the chip profile JSON,
and the SPL comes from a real OpenIPC U-Boot binary for that chip.

### Why real SPL is required

The 64-byte DDR step performs basic DDR initialization, but the SPL does
critical additional setup:
- Clock tree configuration
- DDR timing calibration
- Pin multiplexing
- Voltage regulation

Without real SPL, DDR is not fully initialized and the agent crashes with
a data abort when accessing memory. A `bx lr` stub does NOT work — the
bootrom jumps to SPL (not `bl`), so `lr` is invalid.

### How defib uploads the agent

```
defib boot protocol
    │
    ├── DDR Step ──────── from chip profile JSON (always the same)
    │
    ├── SPL ───────────── from OpenIPC U-Boot binary (first 24KB)
    │                     (auto-downloaded from GitHub releases)
    │
    └── "U-Boot" stage ── agent binary (~4KB, no padding needed)
                          loaded to DDR at 0x41000000 (or 0x81000000)
                          CPU jumps here → agent starts
```

The SPL is extracted from the same OpenIPC U-Boot that defib already
downloads for normal recovery. The agent binary replaces only the
U-Boot payload — the SPL initializes hardware identically to a normal boot.

## Building

```bash
# Default: hi3516ev300
make

# Other SoCs
make SOC=hi3516cv300
make SOC=gk7205v200

# All supported SoCs
make all-socs
```

Requires `arm-none-eabi-gcc` (Arch: `pacman -S arm-none-eabi-gcc arm-none-eabi-newlib`).

### Supported SoCs

| SoC | UART Base | Flash Base | RAM Base | Load Address |
|-----|-----------|------------|----------|-------------|
| hi3516ev300 | 0x12040000 | 0x14000000 | 0x40000000 | 0x41000000 |
| hi3516ev200 | 0x12040000 | 0x14000000 | 0x40000000 | 0x41000000 |
| gk7205v200 | 0x12040000 | 0x14000000 | 0x40000000 | 0x41000000 |
| gk7205v300 | 0x12040000 | 0x14000000 | 0x40000000 | 0x41000000 |
| hi3516cv300 | 0x12100000 | 0x14000000 | 0x80000000 | 0x81000000 |
| hi3516cv500 | 0x12100000 | 0x14000000 | 0x80000000 | 0x81000000 |
| hi3518ev200 | 0x12100000 | 0x14000000 | 0x80000000 | 0x81000000 |
| hi3516cv610 | 0x11040000 | 0x14000000 | 0x40000000 | 0x41000000 |
| hi3519v101 | 0x12100000 | 0x14000000 | 0x80000000 | 0x81000000 |
| hi3520dv200 | 0x20080000 | 0x58000000 | 0x80000000 | 0x81000000 |

Addresses from [qemu-hisilicon](https://github.com/OpenIPC/LoTool) hardware definitions.

`hi3520dv200` is a V1-era DVR/NVR SoC: Cortex-A9 (single core), `0x2xxxxxxx`
peripheral map, and a HISFC350 SPI flash controller (NOT the FMC100 used by
all other supported SoCs). The HISFC350 driver lives in
`spi_flash_hisfc350.c` and is selected via `SPI_DRIVER=hisfc350` in the
per-SoC Makefile stanza.

## Agent Binary Details

- **Size**: ~4 KB code + 1 KB CRC32 table = ~5 KB total (HISFC350 build is
  somewhat larger because of the bank-switching machinery)
- **Runs bare-metal**: no OS, no U-Boot, no libc
- **UART**: ARM PrimeCell PL011 (polled I/O, 115200 8N1). On hi3520dv200 the
  bootrom hands UART running off a slow ~2 MHz reference; we preserve those
  divisors at startup so 115200 keeps working without any CRG poking.
- **Flash**: HiSilicon FMC controller (default) — memory-mapped read,
  register write/erase. V1-era chips (hi3520dv200) use the HISFC350
  controller instead, with the same external API (`flash_read`,
  `flash_erase_sector`, `flash_write_page`, ...) so `main.c` is
  controller-agnostic.
- **Protocol**: COBS framing + CRC-32 per packet

### Startup sequence

1. Disable IRQ/FIQ
2. Invalidate TLBs and caches
3. Disable MMU (U-Boot may have left it enabled)
4. Set up stack (16KB below load address)
5. Clear BSS
6. **Disable hardware watchdog** (SP805) — SPL enables it, must disable or SoC resets in ~30s
7. Initialize UART (PL011, already configured by SPL — just ensure enabled)
8. Send READY packet via COBS protocol
9. Enter command loop (re-sends READY every 2s until first command)

### Important: `-mno-unaligned-access`

The Makefile uses `-mno-unaligned-access` to prevent GCC from generating
`str` (word store) instructions to non-word-aligned addresses. Without this
flag, writing CRC32 bytes at arbitrary offsets in packet buffers causes ARM
data aborts. This was discovered during real hardware testing on hi3516ev300.

## Protocol

COBS-framed binary packets with CRC-32. Frame delimiter: `0x00`.

### Packet format

```
[COBS-encoded payload] [0x00]

Payload before encoding:
[cmd: 1B] [data: 0-1024B] [crc32: 4B LE]
```

### Commands

| Cmd | Name | Host→Device | Device→Host |
|-----|------|-------------|-------------|
| 0x01 | INFO | `{}` | `{jedec[3], flash_size[4], ram_base[4], sector_size[4]}` |
| 0x02 | READ | `{addr[4], size[4]}` | Stream of DATA packets + ACK |
| 0x03 | WRITE | `{addr[4], data[N]}` | ACK |
| 0x04 | ERASE | `{addr[4], size[4]}` | ACK |
| 0x05 | CRC32 | `{addr[4], size[4]}` | `{crc32[4]}` |
| 0x06 | REBOOT | `{}` | (device resets) |
| 0x85 | READY | — | `"DEFIB"` (agent announces itself) |

### READY handshake

After boot, the agent sends READY every 2 seconds until it receives a
command. This handles the case where the host reconnects after the initial
READY was sent (e.g., socket chardev in QEMU, or defib restarting).

## Testing

### Real hardware (verified on hi3516ev300)

```bash
# 1. Upload agent via defib boot protocol
#    (defib uses real SPL from OpenIPC U-Boot + agent as U-Boot stage)
# 2. Agent starts, sends COBS READY packet
# 3. Host communicates via COBS protocol for flash operations
```

### QEMU

```bash
qemu-system-arm -M hi3516ev300 -m 64M -display none -monitor none \
    -chardev socket,id=ser0,path=/tmp/qemu.sock,server=on,wait=off \
    -serial chardev:ser0

# Upload via defib
defib burn -c hi3516ev300 -p socket:///tmp/qemu.sock -f agent-hi3516ev300.bin
```

Note: QEMU's socket chardev drops data when no client is connected.
The agent's READY re-send (every 2s) handles this — connect within a
few seconds after upload.

## Files

```
agent/
├── Makefile        — Cross-compile, per-SoC configs, auto-pad
├── startup.S       — ARM32 entry: IRQ disable, MMU off, TLB flush, BSS clear
├── main.c          — Command loop: INFO, READ, WRITE, ERASE, CRC32, REBOOT
├── uart.c/h        — PL011 UART driver (polled, per-SoC base address)
├── spi_flash.c/h   — HiSilicon FMC flash controller
├── protocol.c/h    — COBS framing + CRC-32 packets
├── cobs.c/h        — COBS encode/decode
└── link.ld         — Linker script (per-SoC load address)
```
