# NAND Flash Backup and Restore with UBI Support

Complete guide to backing up vendor firmware from NAND-based IP cameras and restoring it using defib with proper UBI volume handling.

## Why this matters

Most IP cameras use NAND flash with UBI (Unsorted Block Images) for wear leveling and bad block management. NAND flash chips have **bad blocks** — defective erase blocks that are marked at the factory or develop over time. Each physical chip has bad blocks at **different locations**.

A naive `nand write` command skips bad blocks, shifting all subsequent data. This breaks UBI's internal physical-to-logical block mapping, corrupting UBIFS filesystems. The camera boots, UBI attaches, but UBIFS finds garbage where it expects its superblock:

```
UBIFS error: bad CRC: calculated 0xde13f664, read 0xb30f1910
UBIFS error: bad node at LEB 93:50440
Kernel panic - not syncing: VFS: Unable to mount root fs
```

defib's `restore` command solves this by using **UBI-aware writes** (`ubi write`) that let UBI handle bad block mapping internally. The result: UBIFS mounts cleanly on any chip, regardless of bad block layout.

## Understanding your camera's flash

### NOR vs NAND vs eMMC

| Feature | NOR | NAND | eMMC |
|---------|-----|------|------|
| Typical size | 8-32 MB | 128-512 MB | 4-32 GB |
| Bad blocks | No | Yes | No (managed internally) |
| Partitioning | Raw offsets | MTD + UBI | MBR/GPT |
| Common on | Budget cameras | Mid-range cameras | High-end cameras |
| U-Boot command | `sf` | `nand` | `mmc` |

Check your camera's boot log for clues:
```
SPI Nand(cs 0) ID: 0xc2 0x12 Name:"MX35LF1GE4AB"     # NAND
Check Flash Memory Controller v100 ... Found             # HiSilicon FMC
SPI Nor total size: 16MB                                  # NOR
```

### Partition layout

A typical NAND camera has 5-7 MTD partitions:

```
# cat /proc/mtd
dev:    size   erasesize  name
mtd0: 00100000 00020000 "boot"        # 1MB  - U-Boot bootloader
mtd1: 00400000 00020000 "kernel"      # 4MB  - Linux kernel
mtd2: 00800000 00020000 "rootfs"      # 8MB  - Root filesystem (UBI/UBIFS)
mtd3: 01000000 00020000 "data"        # 16MB - User data (UBI/UBIFS)
mtd4: 01000000 00020000 "upgradefs"   # 16MB - Upgrade staging (UBI/UBIFS)
mtd5: 04000000 00020000 "appfs"       # 64MB - Application (UBI/UBIFS)
mtd6: 01300000 00020000 "exdata"      # 19MB - Extended data (UBI/UBIFS)
```

The `mtdparts` string from U-Boot bootargs encodes this layout:
```
mtdparts=hinand:1M(boot),4M(kernel),8M(rootfs),16M(data),16M(upgradefs),64M(appfs),19M(exdata)
```

### UBI and UBIFS

- **boot** and **kernel** partitions are raw — no UBI, just binary data at fixed offsets
- **rootfs**, **data**, **appfs**, etc. use **UBI** for bad block management
- Inside each UBI partition, one or more **UBI volumes** hold **UBIFS** filesystems
- UBI devices appear as `/dev/ubi0`, `/dev/ubi3`, etc. Volumes as `/dev/ubi0_0`, `/dev/ubi3_0`

## Making a proper backup

### Method 1: From Linux shell (recommended for UBI volumes)

If you have shell access to the running camera (SSH, telnet, or UART console):

```bash
# Step 1: Record the partition layout
cat /proc/mtd > /tmp/mtd_layout.txt

# Step 2: Record U-Boot bootargs (contains mtdparts)
cat /proc/cmdline > /tmp/cmdline.txt

# Step 3: List UBI devices and volumes
ls -la /dev/ubi*

# Step 4: Dump non-UBI partitions (boot, kernel) as raw MTD
dd if=/dev/mtd0 of=/tmp/mtd0 bs=128k
dd if=/dev/mtd1 of=/tmp/mtd1 bs=128k

# Step 5: Dump UBI volumes (UBIFS filesystem images)
# These contain ONLY the logical data — no bad block mapping
dd if=/dev/ubi0_0 of=/tmp/rootfs.ubifs bs=128k    # rootfs volume
dd if=/dev/ubi3_0 of=/tmp/data.ubifs bs=128k       # data volume
dd if=/dev/ubi4_0 of=/tmp/upgradefs.ubifs bs=128k  # upgradefs volume
dd if=/dev/ubi5_0 of=/tmp/appfs.ubifs bs=128k      # appfs volume
dd if=/dev/ubi6_0 of=/tmp/exdata.ubifs bs=128k     # exdata volume
```

> **Why dump `/dev/ubiX_Y` instead of `/dev/mtdN`?**
>
> Raw MTD dumps (`/dev/mtdN`) include UBI erase counter headers and VID headers that encode the physical-to-logical block mapping for THIS specific chip. If you write that dump to a different chip (or the same chip after new bad blocks develop), UBI's mapping is wrong and UBIFS finds corrupted data.
>
> UBI volume dumps (`/dev/ubiX_Y`) contain only the logical filesystem data. When written back via `ubi write`, UBI creates fresh mappings appropriate for the target chip's bad block layout.

### Method 2: From U-Boot console

If you only have U-Boot access (camera is bricked, no Linux):

```bash
# Read partition to RAM, then dump via TFTP
setenv ipaddr 192.168.1.20
setenv serverip 192.168.1.10
nand read 0x82000000 0x0 0x100000     # read mtd0 (boot) to RAM
# Transfer via TFTP to host...
```

Note: U-Boot's `nand read` gives you a raw dump (like `/dev/mtdN`). For UBI partitions, this dump is chip-specific. You can restore it to the SAME chip via `nand write`, but for cross-chip restore you need the UBIFS volume dumps from Method 1.

### Method 3: Using defib dump-flash

For NOR flash cameras, defib can dump the entire flash:

```bash
defib dump-flash -p /dev/ttyUSB0 -o flash_dump.bin --size 16MB
```

## Preparing the restore directory

Organize your backup files in a directory with `mtdN` naming:

```
restore/
  mtd0     # boot partition (raw, from dd if=/dev/mtd0)
  mtd1     # kernel partition (raw, from dd if=/dev/mtd1)
  mtd2     # rootfs (UBIFS image from dd if=/dev/ubi0_0)
  mtd3     # data (UBIFS image from dd if=/dev/ubi3_0)
  mtd4     # upgradefs (UBIFS image from dd if=/dev/ubi4_0)
  mtd5     # appfs (UBIFS image from dd if=/dev/ubi5_0)
  mtd6     # exdata (UBIFS image from dd if=/dev/ubi6_0)
```

defib auto-detects the partition type from the first 4 bytes of each file:

| Magic bytes | Type | Write method |
|-------------|------|--------------|
| `31 18 10 06` | UBIFS superblock | `ubi write` (UBI-aware) |
| `55 42 49 23` | Raw UBI image | `nand write` (raw, chip-specific) |
| Anything else | Raw data | `nand write` (raw) |

## Restoring with defib

### Simple NOR restore

```bash
defib restore -c hi3516ev300 -i ./restore/ -p /dev/ttyUSB0 --power-cycle
```

### NAND restore with UBI volumes

```bash
defib restore -c hi3516av200 \
  -i ./restore/ \
  -p /dev/uart-hi3516av200 \
  --uboot /path/to/u-boot-with-ubi.bin \
  --mtdparts "hinand:1M(boot),4M(kernel),8M(rootfs),16M(data),16M(upgradefs),64M(appfs),19M(exdata)" \
  --power-cycle
```

**Flags explained:**

- `-c hi3516av200` — chip model (determines boot protocol, DDR init sequence)
- `-i ./restore/` — directory with `mtdN` files
- `--uboot` — U-Boot binary with UBI command support (`CONFIG_CMD_UBI=y`) and sufficient heap (`CONFIG_SYS_MALLOC_LEN >= 2MB`). The vendor U-Boot usually lacks UBI commands.
- `--mtdparts` — NAND partition layout from vendor bootargs. Required for UBI operations so defib knows the real partition offsets and names.
- `--power-cycle` — automated PoE power cycling via MikroTik RouterOS

### What happens under the hood

```
Phase 1: Load U-Boot to RAM
   Serial boot protocol → DDR init → SPL → U-Boot
   (22 seconds at 115200 baud for ~200KB U-Boot)

Phase 2: Detect mode
   Camera enters "download command mode" (XHEAD/XCMD protocol)
   or normal U-Boot shell — defib handles both transparently

Phase 3: Detect flash
   Tries: nand info → sf probe 0 → mmc dev 0

Phase 4: Network setup
   Configure IP addresses, wait for PHY link (retries 5x)

Phase 5: Write partitions
   For each partition (boot written LAST):
   ┌─────────────┬─────────────────────────────────────────┐
   │ Non-UBI     │ TFTP → nand erase → nand write          │
   │ (boot,      │                                         │
   │  kernel)    │                                         │
   ├─────────────┼─────────────────────────────────────────┤
   │ UBIFS       │ TFTP → nand erase → ubi part →          │
   │ (rootfs,    │ ubi create VOL_NAME SIZE → ubi write     │
   │  data, etc) │                                         │
   └─────────────┴─────────────────────────────────────────┘

Phase 6: Reset
```

### Why boot is written last

During restore, defib sends `setenv` commands to configure mtdparts and network. Some U-Boot builds auto-save environment to NAND at an offset within the boot partition (typically 0x40000). If boot was written first, these env saves corrupt it. Writing boot last overwrites any corruption with the original dump data.

## The bad block problem explained

```
Source NAND chip:                    Target NAND chip:
┌──────┐ PEB 0: data A              ┌──────┐ PEB 0: data A
├──────┤ PEB 1: data B              ├──────┤ PEB 1: data B
├──────┤ PEB 2: [BAD BLOCK]         ├──────┤ PEB 2: data C  ← shifted!
├──────┤ PEB 3: data C              ├──────┤ PEB 3: data D  ← shifted!
├──────┤ PEB 4: data D              ├──────┤ PEB 4: [BAD BLOCK]
├──────┤ PEB 5: data E              ├──────┤ PEB 5: data E
└──────┘                             └──────┘

nand write skips bad blocks, so data C-D shift position.
UBI headers reference specific PEB numbers → corruption.
```

**With `ubi write`:**

```
ubi write doesn't care about PEB numbers.
It writes logical data to UBI volumes.
UBI maps logical blocks to available physical blocks,
skipping bad blocks automatically.
→ UBIFS mounts correctly on ANY chip.
```

## U-Boot requirements for NAND/UBI restore

The U-Boot loaded to RAM must have:

1. **UBI command support**: `CONFIG_CMD_UBI=y` in U-Boot config
2. **Sufficient heap**: `CONFIG_SYS_MALLOC_LEN` must be at least 2MB (`CONFIG_ENV_SIZE + 2*1024*1024`). The UBI driver needs ~124KB per `vmalloc()` call, and TFTP uses significant heap. With the default 384KB heap, `ubi write` fails with "Cannot start volume update" after TFTP.
3. **Network support**: Working ethernet with correct PHY mode (RMII/RGMII)
4. **NAND support**: SPI NAND driver for the target flash chip

The **vendor U-Boot** typically has network + NAND but lacks UBI commands. A custom-built U-Boot from [OpenIPC u-boot-hi3519v101](https://github.com/OpenIPC/u-boot-hi3519v101) with the above config works.

## Troubleshooting

### "Cannot start volume update"

U-Boot heap too small. Check `CONFIG_SYS_MALLOC_LEN`:
```c
// include/configs/hi3516av200.h
#define CONFIG_SYS_MALLOC_LEN  (CONFIG_ENV_SIZE + 2*1024*1024)
```

Also check that `include/configs/hi-common.h` doesn't override it — wrap with `#ifndef`:
```c
#ifndef CONFIG_SYS_MALLOC_LEN
#define CONFIG_SYS_MALLOC_LEN  (CONFIG_ENV_SIZE + 512*1024)
#endif
```

### UBIFS error -19 (ENODEV) on boot

The UBI volume name doesn't match what the kernel expects. The kernel mounts `root=ubi0:rootfs` — the volume must be named `rootfs`, not `vol0` or `mtd2`. defib uses the partition name from `--mtdparts` as the volume name.

### Boot partition corrupted after restore

The boot partition was written before other partitions, and `setenv` commands during UBI setup overwrote the env area. defib writes boot **last** to avoid this.

### PHY not linked / Network not working

The ethernet PHY needs time to establish link after boot. defib retries ping 5 times with 3-second delays. If it still fails:
- Check IP addresses match your network (`--host-ip`, `--device-ip`)
- Ensure the camera is on the same subnet as the host
- Some cameras need a PHY mode register poke — check vendor U-Boot source

### "Unknown command 'tftpboot'"

Vendor U-Boot uses `tftp` instead of `tftpboot`. defib tries `tftpboot` first, then falls back to `tftp` automatically.

### U-Boot enters download mode instead of shell

HiSilicon chips with PRESTEP0 set `CONFIG_START_MAGIC` ("DOWN") in a register during serial boot. U-Boot detects this and enters `download_process()` instead of the normal shell. defib handles both modes transparently — in download mode it wraps commands in XHEAD/XCMD binary frames.

Use `-t` flag to interact manually:
```bash
defib burn -c hi3516av200 -p /dev/ttyUSB0 --power-cycle -t
# defib> nand info
# Device 0: nand0, sector size 128 KiB
# [OK]
```

## Real-world example

Full restore of hi3516av200 camera (128MB SPI NAND, 7 partitions, 5 UBI volumes):

```
$ sudo defib restore -c hi3516av200 -i ./restore/ \
    --uboot u-boot-hi3516av200-universal.bin \
    --mtdparts "hinand:1M(boot),4M(kernel),8M(rootfs),16M(data),16M(upgradefs),64M(appfs),19M(exdata)" \
    -p /dev/uart-hi3516av200 --power-cycle

Phase 1: Loading U-Boot to RAM
  Sending DDR step ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100%
  Sending SPL      ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100%
  Sending U-Boot   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100%
Phase 2: Detecting U-Boot mode → Download command mode
Phase 3: Detecting flash → nand
Phase 4: Network setup → OK (attempt 2)
Phase 5: Writing flash
  mtd1: 4096KB → 0x100000     nand write    (4.5s)
  mtd2: 5084KB → 0x500000     ubi write     (11.0s) ← UBIFS rootfs
  mtd3: 12896KB → 0xD00000    ubi write     (11.1s) ← UBIFS data
  mtd4: 12896KB → 0x1D00000   ubi write     (11.1s) ← UBIFS upgradefs
  mtd5: 60512KB → 0x2D00000   ubi write     (34.1s) ← UBIFS appfs
  mtd6: 15872KB → 0x6D00000   ubi write     (11.2s) ← UBIFS exdata
  mtd0: 1024KB → 0x0          nand write    (3.5s)  ← boot (LAST)
Restore complete! Device is rebooting.
```

After cold boot — all volumes mount, vendor firmware runs:
```
UBIFS: mounted UBI device 0, volume 0, name "rootfs"    ✓
UBIFS: mounted UBI device 3, volume 0, name "data"      ✓
UBIFS: mounted UBI device 4, volume 0, name "upgradefs" ✓
UBIFS: mounted UBI device 5, volume 0, name "appfs"     ✓
UBIFS: mounted UBI device 6, volume 0, name "exdata"    ✓
(none) login:
Sony IMX385 Sensor 1080p30 Initial OK!
RTSP server is running...
```

Total restore time: ~87 seconds for 113MB across 7 partitions.
