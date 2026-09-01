# CLAUDE.md

Guidance for Claude Code (claude.ai/code) and other AI coding assistants working
in this repository. `AGENTS.md` is a symlink to this file.

> **"Agent" is overloaded here.** In this repo *the agent* almost always means
> the bare-metal ARM32 flash agent that runs on the camera's SoC (`agent/`,
> `src/defib/agent/`, `defib agent <cmd>`, commit scopes `agent:`). Commits and
> issues about "the agent" are about C code on a camera, not about you.

## What is defib

Universal IP camera recovery tool — it unbricks cameras by talking to the SoC's
boot ROM before any OS exists. Two recovery families, selected per chip by the
`RECOVERY` field in its profile:

- **`uart`** — HiSilicon and Goke SoCs (the large majority). The boot ROM accepts
  a serial download; defib drives DDR init, SPL and U-Boot over a USB-serial
  adapter, then flashes over UART or TFTP.
- **`usb`** — Rockchip SoCs (`rv1106` today). The boot ROM has *no* UART download
  path at all: recovery is USB-only, via MaskROM vendor control transfers that
  push a DDR blob and a "usbplug" into SRAM, after which the board speaks a
  USB-MSC-shaped bulk protocol. See `src/defib/rockusb/__init__.py`.

Python 3.11+ async codebase (~15k LOC under `src/`) plus a bare-metal ARM32 C
flash agent (~4k LOC in `agent/`).

## Build & Development Commands

```bash
# Install for development. Add --extra rockchip for the Rockchip USB tests,
# which need pyusb; --extra web installs deps nothing currently imports.
uv sync --extra dev --extra tui --extra rockchip

# Run all Python tests. --ignore is required: testpaths = ["tests"] collects
# tests/fuzz by default.
uv run pytest tests/ -x -v --ignore=tests/fuzz

# Run a single test file or test
uv run pytest tests/test_agent_protocol.py -x -v
uv run pytest tests/test_agent_protocol.py::test_function_name -x -v

# Fuzz tests (property-based with Hypothesis)
uv run pytest tests/fuzz/ -x -v --hypothesis-seed=0

# Lint and type check
uv run ruff check src/ tests/
uv run mypy src/defib/ --ignore-missing-imports

# Agent C tests (host-compiled, no cross toolchain needed)
make -C agent test HOST_CC=gcc

# JS protocol tests — run both; profile-parity guards the web/index.html
# profile duplicate against src/defib/profiles/data/ drift.
node --test web/protocol.test.js web/profile-parity.test.js

# Cross-compile flash agent (ARM32; needs arm-none-eabi-gcc + newlib)
make -C agent                   # default: SOC=hi3516ev300
make -C agent SOC=hi3516cv300   # specific SoC
make -C agent all-socs          # only 4 representative SoCs, not all 10
```

Apart from the single-test and cross-compile lines, those are exactly what the
five CI jobs in `.github/workflows/ci.yml` run (`test`, `fuzz`, `lint`,
`agent-test`, `js-test`). The test matrix is Linux/macOS/Windows × Python
3.11/3.12/3.13. There is no `ruff format` check, and CI does not install
`--extra rockchip`.

## Architecture

### Three-layer design (UART recovery)

1. **Transport** (`src/defib/transport/`) — abstract byte-level communication.
   Protocol code never touches a serial port directly. Six concrete transports:
   `SerialTransport` (real hardware), `MacOSSerialTransport` (macOS ACK-byte and
   stale-buffer workarounds), `SocketTransport` (QEMU), `Rfc2217Transport` and
   `RackTransport` (remote UART bridges), `MockTransport` (unit tests).
   `create_transport()`/`normalize_port_name()` in `serial_platform.py` route the
   `socket://`, `tcp://`, `rfc2217://` and `rack://` URL schemes.

2. **Protocol** (`src/defib/protocol/`) — three boot ROM dialects, all inheriting
   `BootProtocol`, registered via the `@register` decorator and exposed as entry
   points in pyproject.toml:
   - `HiSiliconStandard` — matches **any chip that has a profile JSON**, i.e. all
     112; classic init_bootmode handshake
   - `HiSiliconV500` — `gk7205v500/510/530`, `xm7205v500/510/530`; different handshake
   - `HiSiliconCV6xx` — `hi3516cv608/610/613`, `hi3516dv500`, `hi3519dv500`; multi-stage

   The V500 and CV6xx chips are hardcoded frozensets and **deliberately have no
   profile JSON** — their DDR/SPL data lives in the protocol class. That is the
   only thing keeping the matchers from overlapping, because `find_protocol()`
   returns the *first* class whose `matches()` says yes and the entry points load
   alphabetically: `[HiSiliconCV6xx, HiSiliconStandard, HiSiliconV500]`. Adding a
   profile JSON for a V500-family chip would hand it to `HiSiliconStandard` and
   silently break it. `find_protocol()` strips any `:variant` suffix first.

   `rv1106` does have a profile, so it matches `HiSiliconStandard` too — the CLI
   checks the profile's `RECOVERY` field before it ever calls `find_protocol()`.

3. **Recovery** (`src/defib/recovery/`) — session orchestrator. Emits typed
   events (`ProgressEvent`, `LogEvent`, `HandshakeResult`, `RecoveryResult`)
   consumed by CLI, TUI and Web without tight coupling. `rack_fastboot.py`
   offloads the SPL upload to a rack pod when the link is too high-latency for
   the per-frame ACK loop.

### Rockchip USB recovery (`src/defib/rockusb/`)

Deliberately *not* a `BootProtocol` — neither MaskROM control transfers nor
CBW/CSW bulk are byte streams, so nothing here registers with `defib.protocols`.
`maskrom.py` (SRAM upload), `protocol.py` (opcodes, CBW/CSW), `device.py` (pyusb
enumeration, MASKROM vs LOADER mode), `loader.py` (RKBOOT containers), `codec.py`
(RK CRC16 + RC4), `recovery.py` (`RockchipRecovery`). Reached through
`--ddr`/`--usbplug` (or `--loader`) and `--usb-path` on `burn` and `install`;
the loader blobs are vendor rkbin files the user supplies, not bundled.

### Flash agent (`agent/`)

Bare-metal ARM32 C loaded onto the camera after SPL boot. COBS-framed binary
protocol with CRC-32, 1024-byte max payload. It comes up at **115200** (the rate
the boot ROM left the UART at) and is switched to 921600 by `CMD_SET_BAUD` after
the handshake, reverting to 115200 after ~30 s idle. 13 commands: `INFO 0x01`,
`READ 0x02`, `WRITE 0x03`, `ERASE 0x04`, `CRC32 0x05`, `REBOOT 0x06`,
`SELFUPDATE 0x07`, `SET_BAUD 0x08`, `SCAN 0x09`, `FLASH_PROGRAM 0x0A`,
`FLASH_STREAM 0x0B`, `MARK_BAD 0x0C`, `MEMBW 0x0D`. `agent/protocol.h` and
`src/defib/agent/protocol.py` must stay in lockstep; the client negotiates
optional features through a capability bitmask (`client.py`).

Backends: `spi_flash.c` (fmc100), `spi_flash_hisfc350.c` (V1-era parts),
`emmc_himci.c`. Ten SoCs are supported; each has its own `ifeq` stanza in
`agent/Makefile` carrying `LOAD_ADDR` (and `SPI_DRIVER` where it differs).
`link.ld` itself is generic — it just places `. = LOAD_ADDR`.

### Other key modules

- **Profiles** (`src/defib/profiles/`) — 112 JSON SoC definitions in `data/`,
  validated with Pydantic (`schema.py`). `defib list-chips` shows 123: those 112
  plus the 11 hardcoded V500/CV6xx chips that have no profile file.
  `hi3516av300.json` additionally declares a board `variant`, selected as
  `hi3516av300:emmc`. A profile file whose entire contents are another filename
  is an **alias** (`hi3516ev300.json` is just `hi3516ev200.json`).
- **CLI** (`src/defib/cli/`) — Typer app, one large `app.py`. Commands: `burn`,
  `install`, `restore`, `dump-flash`, `detect`, `capture`, `replay`, `network`,
  `ports`, `list-chips`, `list-interfaces`, `tui`, plus the `agent` sub-app
  (`upload`, `flash`, `info`, `read`, `write`, `scan`, `membw`).
- **Power** (`src/defib/power/`) — `routeros` (MikroTik PoE, default), `vectis`,
  `rack`, chosen by `DEFIB_POWER_TYPE`.
- **TUI** (`src/defib/tui/`) — Textual UI, including the Flash Doctor screen.
- **Web** (`web/`) — WebSerial browser UI: standalone HTML/JS, no build step,
  deployed to GitHub Pages. `src/defib/web/` is an empty placeholder package —
  there is no FastAPI server, and the `web` extra is vestigial.
- **Network** (`src/defib/network/`) — multi-file TFTP server with
  filename-based partition routing, plus temporary static-IP management and
  U-Boot device discovery.
- **Firmware** (`src/defib/firmware.py`) — downloads OpenIPC releases from
  GitHub, caches under `XDG_CACHE_HOME`.
- **Capture** (`src/defib/capture/`) — record/replay UART sessions in `.dcap`.
- Loose modules worth knowing: `flashdump.py` (dump flash through a U-Boot
  console), `ubi.py` (extract UBIFS volumes from raw UBI), `uboot_env.py`,
  `serial_ports.py`.

### Plugin registration

Protocols are real plugins: subclass `BootProtocol`, add `@register`, and either
ship in-tree or publish an entry point in the `defib.protocols` group —
`registry.py` imports them on first use.

**Power controllers are not.** Despite the `[project.entry-points."defib.power"]`
block in pyproject.toml, nothing reads that group;
`power_controller_from_env()` in `src/defib/power/factory.py` is a hardcoded
`if/elif` on `DEFIB_POWER_TYPE`. A new controller needs a branch added there.

## Working with real hardware

**Back up before you write.** This is the whole rule. Cameras arrive with vendor
firmware that is often unobtainable anywhere else; an erase is final.

**There are no guard rails.** The CLI has no confirmation prompts, no
`--dry-run`, and no `--force` — not one `typer.confirm` or `input()` call exists.
Every destructive command begins erasing the moment it is invoked. Never run one
speculatively, to see an error message, or to "check whether the board responds".

| Safety | Commands |
|---|---|
| Host-only, never touches a device | `list-chips`, `ports`, `list-interfaces`, `replay` |
| Talks to the device; RAM-only or read-only | `detect`, `capture`, `dump-flash`, `burn`, `agent upload\|info\|read\|scan\|membw`, `network` (serves a file; the *device* decides to write) |
| **Irreversibly erases and writes flash** | `install`, `restore`, `agent flash`, `agent write`, TUI Flash Doctor write paths |

`burn` only uploads into RAM, but it still power-cycles the board and can leave
it parked in download mode — recoverable with another power cycle.

Taking a backup first:

```bash
# Via a running U-Boot console (run `defib burn` first, or attach to a live one)
defib dump-flash -p /dev/ttyUSB0 -o backup.bin --size 16MB

# Faster, via the bare-metal agent (address and size auto-detected)
defib agent upload -c hi3516ev300 -p /dev/ttyUSB0
defib agent read   -p /dev/ttyUSB0 -o backup.bin
```

Safety properties already built in — do not re-derive or undo them:

- `restore` writes the **boot partition last**, so an interrupted restore usually
  leaves a bootable bootloader (`cli/app.py`, "Write boot partition (offset 0) LAST").
- The env partition is preserved unless `--wipe-env`; wiping it loses `ethaddr`
  and the MAC falls back to OpenIPC's compiled-in `00:00:23:34:45:66`.
- `agent flash` skips all-`0xFF` sectors and verifies CRC32 (`--no-verify` opts out).
- `FlashPartition` carries `sectors` as well as `lba` specifically so an oversized
  image cannot be written through into the next partition.
- The web UI refuses frame-blast SoCs (`FRAME_BLAST_SOCS` in `web/protocol.js`)
  instead of failing at the wire.

### Serial ports, baud rates, privileges

- Your user needs write access to the port — the `dialout` group on most Linux
  distros (`uucp` on Arch). Otherwise every command fails with a permission error.
- Boot ROM and U-Boot run at **115200 8N1**; only the flash agent moves to 921600.
- `defib install`, `defib restore` and `defib network` need **root** for TFTP port
  69 and NIC IP assignment. `--tftp-via pod` avoids that but requires
  `DEFIB_POWER_TYPE=rack`.
- Docs and examples use `/dev/uart-<CAMERA-NAME>` paths. That is a **udev symlink
  convention this repo relies on but does not create for you** — `serial_ports.py`
  prefers `/dev/uart-*`, then `/dev/serial/by-id/*`, then `/dev/serial/by-path/*`,
  and `--power-cycle` finds the PoE port by matching that name against switch
  interface comments. Plain `/dev/ttyUSB0` works fine without it.
- Vectis emits only a fixed-width reset pulse, so `defib restore` (which needs
  independent power off/on) does not work over it.

## Testing without hardware

The entire test suite runs offline — there are no `hardware`/`integration`
pytest markers and no test opens a real port. Three levels of fidelity:

1. **`MockTransport`** — the default for unit tests, available as the
   `mock_transport` fixture (`tests/conftest.py`). Script the device's replies
   with `enqueue_rx()` / `enqueue_rx_chunks()`, then assert on `tx_log` /
   `all_tx_data()`. Pass `flush_clears_buffer=False` for protocols that call
   `flush_input()` mid-stage.

2. **QEMU** via `SocketTransport` — [qemu-hisilicon](https://github.com/OpenIPC/qemu-hisilicon)
   emulates the boot ROM when started without `-kernel`:

   ```bash
   qemu-system-arm -M hi3516ev300 -m 64M -nographic \
       -chardev socket,id=ser0,path=/tmp/qemu-hisi.sock,server=on,wait=off \
       -serial chardev:ser0
   defib burn -c hi3516ev300 -p socket:///tmp/qemu-hisi.sock
   ```

   Standard-protocol chips only; V500 and CV6xx are not emulated. The wire spec
   QEMU implements is `docs/qemu_hisilicon_spec.md`.

3. **`.dcap` capture/replay** — record a real session once, work offline after.
   Note `defib replay` only *prints* a capture; feeding one back into a protocol
   means constructing `ReplayTransport` (`src/defib/capture/replayer.py`) from
   Python, as `tests/test_capture_recorder.py` does.

If a change can only be validated on hardware, say so plainly rather than
implying it was tested.

## Adding a new chip

This is the most error-prone path in the repo — it has already caused one silent
regression (#121).

1. Add `src/defib/profiles/data/<chip>.json`. Field names are SCREAMING_CASE
   aliases inherited from the original HiSilicon burn tool.
   - `RECOVERY: "uart"` (the default) **requires** `DDRSTEP0`, `ADDRESS`,
     `FILELEN`, `STEPLEN` — enforced by a Pydantic model validator. Optional:
     `PRESTEP0`, `PRESTEP1`, `SRAMLIMIT` (hard SPL upload ceiling), `SPL_BLOB`.
   - `RECOVERY: "usb"` supplies none of those, and instead needs
     `USB_RECOVERY_IDS` (product ids seen *only* in recovery mode — a booted
     Luckfox shows `2207:0019` and must not match), `LOADER_DDR`,
     `LOADER_USBPLUG`, `PARTITIONS`. See `rv1106.json`.
   - A file containing only another profile's filename is an alias.
   - **Exception:** chips in the V500 or CV6xx families must *not* get a profile
     JSON. They are listed in frozensets in their protocol class, and a profile
     file would make `HiSiliconStandard` match them first (see Protocol above).
2. **Update the `PROFILES` blob in `web/index.html`.** The web UI ships as static
   files and carries a hand-maintained copy of the profile data; nothing
   regenerates it. Then run `node --test web/profile-parity.test.js`, which
   exists precisely to catch this drift.
3. If the chip needs it, add it to `FRAME_BLAST_SOCS` in `web/protocol.js`.
4. For flash-agent support, add a per-SoC stanza to `agent/Makefile` with the
   right `LOAD_ADDR` (and `SPI_DRIVER = hisfc350` for V1-era parts), then
   `make -C agent SOC=<soc>`.
5. `uv run pytest tests/test_profiles.py -x` walks the whole data directory.

Contributing a profile you could not test is fine — say so in the commit
message, as `500c95d` ("untested, mirrors gk7205v200") does.

## Code and commit conventions

- Ruff, line length 100, default rule set.
- mypy strict. Two overrides: `defib.tui.*` sets `disallow_subclassing_any = false`;
  `usb`/`usb.*` sets `ignore_missing_imports` (pyusb ships no stubs and is an
  optional extra).
- asyncio throughout, `asyncio_mode = "auto"` in pytest.
- Commit subject is `scope: imperative summary` — *not* Conventional Commits, so
  no `feat:`/`fix:`/`chore:` prefixes. Scopes are areas or chips: `web:`,
  `agent:`, `install:`, `rockusb:`, `dump-flash:`, `hi3516av300:`, and compound
  forms like `agent/protocol:` or `install/restore:`.
- Commit **bodies carry the real documentation**: why, the exact command, and —
  when it applies — the hardware verification evidence ("Verified end to end on a
  Luckfox Pico Max… byte-identical to a backup taken over SSH"). Write them.
- `(#NN)` is appended by GitHub's squash-merge. Do not hand-write it on a branch.
- Note third-party licence provenance in the body when protocol behaviour was
  derived from another project.
- Default branch is `master`; push only to `origin`.
