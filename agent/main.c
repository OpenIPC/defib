/*
 * defib flash agent — bare-metal main loop.
 * Receives commands from host via COBS-framed UART protocol.
 */

#include <stdint.h>
#include "uart.h"
#include "emmc_himci.h"
#include "protocol.h"
#include "spi_flash.h"

static flash_info_t flash_info;

/* --- Diagnostic: data abort handler --- */

static void uart_puthex(uint32_t val) {
    static const char hex[] = "0123456789abcdef";
    uart_putc('0'); uart_putc('x');
    for (int i = 28; i >= 0; i -= 4)
        uart_putc(hex[(val >> i) & 0xF]);
}

void data_abort_handler(uint32_t dfar, uint32_t dfsr, uint32_t pc) {
    uart_puts("\r\n!ABORT addr=");
    uart_puthex(dfar);
    uart_puts(" dfsr=");
    uart_puthex(dfsr);
    uart_puts(" pc=");
    uart_puthex(pc);
    uart_puts("\r\n");
}

#ifndef RAM_BASE
#define RAM_BASE 0x40000000
#endif

#ifndef FLASH_MEM
#define FLASH_MEM 0x14000000
#endif

/* Watchdog base address (SP805 compatible) — set via -DWDT_BASE=... */
#ifndef WDT_BASE
#define WDT_BASE 0x12030000
#endif

/* SYSCTRL software-reset register — set via -DSYSCTRL_REBOOT=... */
#ifndef SYSCTRL_REBOOT
#define SYSCTRL_REBOOT 0x12020004
#endif

#define WDT_LOAD    (*(volatile uint32_t *)(WDT_BASE + 0x000))
#define WDT_CONTROL (*(volatile uint32_t *)(WDT_BASE + 0x008))
#define WDT_INTCLR  (*(volatile uint32_t *)(WDT_BASE + 0x00C))
#define WDT_LOCK    (*(volatile uint32_t *)(WDT_BASE + 0xC00))
#define WDT_UNLOCK_KEY 0x1ACCE551

/* Safety limits */
#define MAX_READ_SIZE   (32 * 1024 * 1024)  /* 32MB max per READ */
#define MAX_UPDATE_SIZE (256 * 1024)         /* 256KB max self-update */

static uint8_t cmd_buf[MAX_PAYLOAD + 16];

static void watchdog_disable(void) {
    /* Disable all watchdogs we know about */
    WDT_LOCK = WDT_UNLOCK_KEY;
    WDT_CONTROL = 0;
    WDT_INTCLR = 1;
    WDT_LOAD = 0xFFFFFFFF;
    WDT_LOCK = 0;

    /* Also try CRG watchdog clock gate — disable WDT clock entirely.
     * On hi3516ev300, CRG base = 0x12010000.
     * WDT clock register varies by SoC, but disabling the clock
     * is the most reliable way to kill the watchdog. */
#if defined(WDT_BASE) && (WDT_BASE == 0x12030000)
    /* hi3516ev300/ev200/gk7205v200: CRG register for WDT */
    /* Try writing 0 to potential WDT clock enable bits */
    volatile uint32_t *crg = (volatile uint32_t *)0x12010000;
    /* Common pattern: WDT soft reset + clock gate in CRG */
    /* Offset varies, try known ones */
#endif
}

static uint32_t read_le32(const uint8_t *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static void write_le32(uint8_t *p, uint32_t v) {
    p[0] = (v >> 0) & 0xFF;
    p[1] = (v >> 8) & 0xFF;
    p[2] = (v >> 16) & 0xFF;
    p[3] = (v >> 24) & 0xFF;
}

/* Check if address range is in safe readable memory */
static int flash_readable = 0;  /* Set to 1 after flash init succeeds */

static int addr_readable(uint32_t addr, uint32_t size) {
    if (size == 0 || (addr + size) <= addr) return 0;  /* Overflow */
    /* RAM region: RAM_BASE to RAM_BASE + 128MB */
    if (addr >= RAM_BASE && (addr + size) <= (RAM_BASE + 128 * 1024 * 1024))
        return 1;
    /* I/O register regions — V3+/V4+ default whitelist. */
    if (addr >= 0x10000000 && (addr + size) <= 0x10001000) return 1; /* FMC regs */
    if (addr >= 0x12010000 && (addr + size) <= 0x12020000) return 1; /* CRG */
    if (addr >= 0x12020000 && (addr + size) <= 0x12030000) return 1; /* SYS_CTRL */
    /* Per-SoC controller regions (FMC + CRG + UART) — covers V1-era
     * chips where these don't sit in the default whitelist (e.g.
     * hi3520dv200 has FMC at 0x10010000, CRG at 0x20030000, UART at
     * 0x20080000). 4 KiB window per controller is enough for any
     * HiSilicon SPI-flash controller. UART access is needed to
     * verify what divisors the bootrom programmed. */
    if (addr >= FMC_BASE  && (addr + size) <= (FMC_BASE  + 0x1000)) return 1;
    if (addr >= CRG_BASE  && (addr + size) <= (CRG_BASE  + 0x1000)) return 1;
    if (addr >= UART_BASE && (addr + size) <= (UART_BASE + 0x1000)) return 1;
#ifdef EMMC_BASE
    /* DesignWare MMC host controller registers — needed to drive the eMMC
     * reader (CMD/CMDARG/RESP0..3/RINTSTS/STATUS/FIFO at 0x000..0x200). */
    if (addr >= EMMC_BASE && (addr + size) <= (EMMC_BASE + 0x1000)) return 1;
#endif
#ifdef IO_CTRL0_BASE
    /* I/O pinmux block, used by configure_emmc_pins. */
    if (addr >= IO_CTRL0_BASE && (addr + size) <= (IO_CTRL0_BASE + 0x1000)) return 1;
#endif
#if defined(BOOTROM_BASE) && defined(BOOTROM_SIZE)
    /* Mask ROM at chip-defined base — needed to read the bootrom's
     * pinmux table baked into ROM. Read-only, safe to expose. */
    if (addr >= BOOTROM_BASE && (addr + size) <= (BOOTROM_BASE + BOOTROM_SIZE))
        return 1;
#endif
    /* Flash memory-mapped window — only after flash_init succeeds.
     * Window size matches the identified medium: SPI NOR is typically
     * 8-32 MiB (sub-32-bit), eMMC can be GiB but is clipped at 4 GiB-1
     * to fit a uint32_t. */
    if (flash_readable && addr >= FLASH_MEM &&
        (uint64_t)(addr - FLASH_MEM) + size <= (uint64_t)flash_info.size)
        return 1;
    return 0;
}

/* Agent protocol version — increment on protocol changes.
 *   v3 added: flash_mem at INFO bytes 24..27 (so the host knows which
 *   memory-mapped flash window CMD_CRC32/CMD_READ should target on
 *   SoCs where it isn't 0x14000000 — e.g. hi3520dv200 has it at
 *   0x58000000).
 *   v4 added: CMD_MEMBW for bare-metal DDR bandwidth measurement
 *   (ARMv7 only; ACK_FLASH_ERROR on ARMv5). */
#define AGENT_VERSION       4

/* Capability flags — advertise supported features */
#define CAP_FLASH_STREAM    (1 << 0)  /* CMD_FLASH_STREAM with double-buffer */
#define CAP_SECTOR_BITMAP   (1 << 1)  /* 0xFF sector skip in FLASH_STREAM */
#define CAP_PAGE_SKIP       (1 << 2)  /* 0xFF page skip in programming */
#define CAP_SET_BAUD        (1 << 3)  /* CMD_SET_BAUD for high-speed UART */
#define CAP_REBOOT          (1 << 4)  /* CMD_REBOOT */
#define CAP_SELFUPDATE      (1 << 5)  /* CMD_SELFUPDATE */
#define CAP_SCAN            (1 << 6)  /* CMD_SCAN */
#ifndef CPU_ARM926
#define CAP_MEMBW           (1 << 7)  /* CMD_MEMBW (ARMv7 PMU cycle counter) */
#else
#define CAP_MEMBW           0
#endif

#define AGENT_CAPS (CAP_FLASH_STREAM | CAP_SECTOR_BITMAP | CAP_PAGE_SKIP | \
                    CAP_SET_BAUD | CAP_REBOOT | CAP_SELFUPDATE | CAP_SCAN | \
                    CAP_MEMBW)

static void handle_info(void) {
    uint8_t resp[28];
    /* JEDEC ID in first 4 bytes (3 bytes + padding) */
    resp[0] = flash_info.jedec_id[0];
    resp[1] = flash_info.jedec_id[1];
    resp[2] = flash_info.jedec_id[2];
    resp[3] = flash_info.flash_type;   /* was reserved padding */
    write_le32(&resp[4], flash_info.size);
    write_le32(&resp[8], RAM_BASE);
    write_le32(&resp[12], flash_info.sector_size);  /* NOR=64K, NAND=128K */
    write_le32(&resp[16], AGENT_VERSION);
    write_le32(&resp[20], AGENT_CAPS);
    write_le32(&resp[24], FLASH_MEM);
    proto_send(RSP_INFO, resp, 28);
}

static void handle_read(const uint8_t *data, uint32_t len) {
    if (len < 8) { proto_send_ack(ACK_CRC_ERROR); return; }

    uint32_t addr = read_le32(&data[0]);
    uint32_t size = read_le32(&data[4]);

    /* Bounds check */
    if (size == 0 || size > MAX_READ_SIZE || !addr_readable(addr, size)) {
        proto_send_ack(ACK_FLASH_ERROR);
        return;
    }

    /* I/O registers require 32-bit word-aligned access (ldr not ldrb).
     * RAM and flash can use byte access. Cover the V3+/V4+/V5/V6
     * peripheral block (0x10000000..0x13000000) as well as the V1-era
     * regions actually used by V1 SoCs (FMC, CRG, UART). */
    int io_region = (addr >= 0x10000000 && addr < 0x13000000)
                 || (addr >= FMC_BASE  && addr < FMC_BASE  + 0x1000)
                 || (addr >= CRG_BASE  && addr < CRG_BASE  + 0x1000)
                 || (addr >= UART_BASE && addr < UART_BASE + 0x1000);

    uint16_t seq = 0;
    uint32_t offset = 0;
    uint8_t pkt[MAX_PAYLOAD];

    int emmc_path = 0;
#ifdef EMMC_BASE
    if (flash_readable && flash_info.flash_type == FLASH_TYPE_EMMC
        && addr >= FLASH_MEM
        && (uint64_t)(addr - FLASH_MEM) + size <= (uint64_t)flash_info.size) {
        /* MVP eMMC read: block-aligned only. addr - FLASH_MEM = LBA byte
         * offset; both that offset and `size` must be 512-byte aligned.
         * uint64_t arithmetic above because FLASH_MEM + flash_info.size
         * would overflow uint32_t when capacity approaches 4 GiB. */
        uint32_t lba_off = addr - FLASH_MEM;
        if ((lba_off & 511u) || (size & 511u)) {
            proto_send_ack(ACK_FLASH_ERROR);
            return;
        }
        emmc_path = 1;
    }
#endif

    while (offset < size) {
        uint32_t chunk = size - offset;
        if (chunk > MAX_PAYLOAD - 2) chunk = MAX_PAYLOAD - 2;
#ifdef EMMC_BASE
        /* eMMC streams block-by-block; cap chunk at one block. */
        if (emmc_path && chunk > 512) chunk = 512;
#endif

        pkt[0] = (seq >> 0) & 0xFF;
        pkt[1] = (seq >> 8) & 0xFF;
        if (io_region) {
            /* Word-aligned 32-bit reads, split into bytes */
            for (uint32_t i = 0; i < chunk; i += 4) {
                uint32_t word_addr = (addr + offset + i) & ~3u;
                uint32_t val = *(volatile uint32_t *)word_addr;
                uint32_t byte_off = (addr + offset + i) & 3;
                for (uint32_t j = 0; j < 4 && (i + j) < chunk; j++)
                    pkt[2 + i + j] = (val >> ((byte_off + j) * 8)) & 0xFF;
            }
#ifdef EMMC_BASE
        } else if (emmc_path) {
            /* eMMC: read one block via CMD17 + FIFO drain. */
            uint8_t blk[512];
            uint32_t block_no = (addr - FLASH_MEM + offset) / 512u;
            if (emmc_read_block(block_no, blk) != 0) {
                proto_send_ack(ACK_FLASH_ERROR);
                return;
            }
            for (uint32_t i = 0; i < chunk; i++) pkt[2 + i] = blk[i];
#endif
        } else if (flash_readable && addr >= FLASH_MEM &&
                   (uint64_t)(addr - FLASH_MEM) + size <= (uint64_t)flash_info.size) {
            /* Register-based flash read — boot mode window wraps at 1MB */
            uint8_t tmp[MAX_PAYLOAD];
            flash_read(addr - FLASH_MEM + offset, tmp, chunk);
            for (uint32_t i = 0; i < chunk; i++)
                pkt[2 + i] = tmp[i];
        } else {
            const uint8_t *ptr = (const uint8_t *)addr;
            for (uint32_t i = 0; i < chunk; i++)
                pkt[2 + i] = ptr[offset + i];
        }

        proto_send(RSP_DATA, pkt, 2 + chunk);
        offset += chunk;
        seq++;
    }
    proto_send_ack(ACK_OK);
}

static void handle_crc32_cmd(const uint8_t *data, uint32_t len) {
    if (len < 8) { proto_send_ack(ACK_CRC_ERROR); return; }

    uint32_t addr = read_le32(&data[0]);
    uint32_t size = read_le32(&data[4]);

    if (size == 0 || size > MAX_READ_SIZE || !addr_readable(addr, size)) {
        proto_send_ack(ACK_FLASH_ERROR);
        return;
    }

    uint32_t c;
    /* Route flash reads through register-based path — boot mode memory
     * window wraps at 1MB on some SoCs. */
    if (flash_readable && addr >= FLASH_MEM &&
        (addr + size) <= (FLASH_MEM + flash_info.size)) {
        c = flash_crc32(addr - FLASH_MEM, size);
    } else {
        const uint8_t *ptr = (const uint8_t *)addr;
        c = crc32(0, ptr, size);
    }
    uint8_t resp[4];
    write_le32(resp, c);
    proto_send(RSP_CRC32, resp, 4);
}

/*
 * CMD_MEMBW: DDR bandwidth test. ARMv7 (Cortex-A7) only.
 *
 * Request:  [size:4LE][iters:4LE][addr:4LE]
 *   size = 0  → 4 MiB default; otherwise must be 256B-aligned, ≤ 16 MiB
 *   iters = 0 → 8 default; max 256
 *   addr = 0  → RAM_BASE + MEMBW_SCRATCH_OFF (auto-pick)
 *
 * Response: [base:4LE][size:4LE][iters:4LE][timer_hz:4LE]
 *           [memset_ticks:4LE][read_ticks:4LE][memcpy_ticks:4LE][cpu_arch:4LE]
 *
 *   timer_hz = CCNT frequency in Hz, calibrated against the architectural
 *              generic timer; 0 if CNTFRQ wasn't set up. Host can still
 *              compute cycles/byte (CPU-clock-invariant) when timer_hz==0.
 *
 * Cache state: MMU is on with DDR mapped as write-back / write-allocate
 * (see startup.S page-table fill). Test runs cached — apples-to-apples
 * with userspace memcpy/memset, with the buffer sized well above L1+L2.
 */
#ifndef CPU_ARM926
static inline void pmccntr_init(void) {
    uint32_t v;
    asm volatile("mrc p15, 0, %0, c9, c12, 0" : "=r"(v));
    v |= (1u << 0);            /* E: enable all counters */
    v |= (1u << 2);            /* C: reset CCNT */
    asm volatile("mcr p15, 0, %0, c9, c12, 0" :: "r"(v));
    asm volatile("mcr p15, 0, %0, c9, c12, 1" :: "r"(0x80000000u));
    asm volatile("isb");
}

static inline uint32_t pmccntr_read(void) {
    uint32_t v;
    asm volatile("isb\n\t"
                 "mrc p15, 0, %0, c9, c13, 0" : "=r"(v));
    return v;
}

/* Calibrate CCNT (CPU cycles) against CNTPCT (architectural timer, fixed
 * frequency from CNTFRQ). Returns CCNT ticks per second, or 0 if CNTFRQ
 * wasn't initialised by an earlier boot stage. */
static uint32_t pmccntr_calibrate_hz(void) {
    uint32_t cntfrq;
    asm volatile("mrc p15, 0, %0, c14, c0, 0" : "=r"(cntfrq));
    /* Sanity: most hi-silicon BL1 sets this to 24 MHz. Anything outside
     * 1 MHz..100 MHz is almost certainly an uninitialised register. */
    if (cntfrq < 1000000u || cntfrq > 100000000u) return 0;

    uint32_t lo0, hi0, lo1, hi1;
    asm volatile("mrrc p15, 0, %0, %1, c14" : "=r"(lo0), "=r"(hi0));
    uint32_t target = cntfrq / 100;   /* 10 ms window */
    pmccntr_init();
    uint32_t c0 = pmccntr_read();
    do {
        asm volatile("mrrc p15, 0, %0, %1, c14" : "=r"(lo1), "=r"(hi1));
    } while ((lo1 - lo0) < target);
    uint32_t c1 = pmccntr_read();
    return (c1 - c0) * 100u;
}

/* Write 8 words per stm — 32 B per loop iteration. r4-r11 are AAPCS
 * callee-saved; listing them as clobbers makes GCC push/pop them in
 * the prologue. */
static void __attribute__((noinline)) membw_memset(uint32_t addr, uint32_t bytes) {
    asm volatile(
        "mov r4, %[v]\n\t"
        "mov r5, %[v]\n\t"
        "mov r6, %[v]\n\t"
        "mov r7, %[v]\n\t"
        "mov r8, %[v]\n\t"
        "mov r9, %[v]\n\t"
        "mov r10, %[v]\n\t"
        "mov r11, %[v]\n\t"
        "1:\n\t"
        "stmia %[p]!, {r4, r5, r6, r7, r8, r9, r10, r11}\n\t"
        "cmp %[p], %[end]\n\t"
        "blo 1b\n\t"
        : [p] "+r"(addr)
        : [end] "r"(addr + bytes), [v] "r"(0xA5A5A5A5u)
        : "r4", "r5", "r6", "r7", "r8", "r9", "r10", "r11",
          "cc", "memory"
    );
}

/* Read 8 words per ldm. No store — pure read bandwidth. */
static void __attribute__((noinline)) membw_read(uint32_t addr, uint32_t bytes) {
    asm volatile(
        "1:\n\t"
        "ldmia %[p]!, {r4, r5, r6, r7, r8, r9, r10, r11}\n\t"
        "cmp %[p], %[end]\n\t"
        "blo 1b\n\t"
        : [p] "+r"(addr)
        : [end] "r"(addr + bytes)
        : "r4", "r5", "r6", "r7", "r8", "r9", "r10", "r11",
          "cc", "memory"
    );
}

/* Copy 8 words per ldm/stm pair — 32 B in, 32 B out per iteration. */
static void __attribute__((noinline)) membw_memcpy(uint32_t dst, uint32_t src, uint32_t bytes) {
    asm volatile(
        "1:\n\t"
        "ldmia %[s]!, {r4, r5, r6, r7, r8, r9, r10, r11}\n\t"
        "stmia %[d]!, {r4, r5, r6, r7, r8, r9, r10, r11}\n\t"
        "cmp %[s], %[end]\n\t"
        "blo 1b\n\t"
        : [s] "+r"(src), [d] "+r"(dst)
        : [end] "r"(src + bytes)
        : "r4", "r5", "r6", "r7", "r8", "r9", "r10", "r11",
          "cc", "memory"
    );
}
#endif /* !CPU_ARM926 */

#define MAX_MEMBW_SIZE    (16u * 1024u * 1024u)
/* Agent footprint guard: protect [AGENT_LOAD_ADDR - 64 KB, AGENT_LOAD_ADDR
 * + 8 MiB) from the test buffer. The lower margin covers the 16 KB stack
 * that lives below _start; the upper margin (8 MiB) is generous head-room
 * for .text/.data/.bss including the 16 KB-aligned page table. The default
 * scratch sits at AGENT_LOAD_ADDR + 8 MiB so even 16 MiB × memcpy (32 MiB
 * total span) fits inside the 128 MiB cached DDR window. */
#define MEMBW_AGENT_GUARD_LO  ((uint32_t)AGENT_LOAD_ADDR - 0x10000u)
#define MEMBW_AGENT_GUARD_HI  ((uint32_t)AGENT_LOAD_ADDR + 0x800000u)
#define MEMBW_DEFAULT_ADDR    ((uint32_t)AGENT_LOAD_ADDR + 0x800000u)

static void handle_membw(const uint8_t *data, uint32_t len) {
#ifdef CPU_ARM926
    (void)data; (void)len;
    /* ARMv5 (ARM926EJ-S) has a different PMU register layout. Out of
     * scope — the motivating use case (gk7205v300 DDR fabric audit) is
     * ARMv7. */
    proto_send_ack(ACK_FLASH_ERROR);
#else
    if (len < 12) { proto_send_ack(ACK_CRC_ERROR); return; }

    uint32_t size  = read_le32(&data[0]);
    uint32_t iters = read_le32(&data[4]);
    uint32_t addr  = read_le32(&data[8]);

    if (size == 0)  size  = 4u * 1024u * 1024u;
    if (iters == 0) iters = 8;
    if (addr == 0)  addr  = MEMBW_DEFAULT_ADDR;

    if (iters > 256 || size > MAX_MEMBW_SIZE || (size & 0xFFu) != 0) {
        proto_send_ack(ACK_FLASH_ERROR); return;
    }
    /* Fit dst = addr + size and src = addr inside the cached DDR
     * window (128 MiB from RAM_BASE per startup.S page-table fill). */
    if (addr < RAM_BASE) { proto_send_ack(ACK_FLASH_ERROR); return; }
    uint32_t off = addr - RAM_BASE;
    if (off + 2u * size > 128u * 1024u * 1024u) {
        proto_send_ack(ACK_FLASH_ERROR); return;
    }
    /* Reject scratch ranges that would overlap the agent's own footprint
     * (its code, stack, page table). memcpy would otherwise overwrite
     * the running agent and the device would hang. */
    uint32_t scratch_end = addr + 2u * size;
    if (scratch_end > MEMBW_AGENT_GUARD_LO &&
        addr        < MEMBW_AGENT_GUARD_HI) {
        proto_send_ack(ACK_FLASH_ERROR); return;
    }

    uint32_t timer_hz = pmccntr_calibrate_hz();

    uint32_t t0, t1;

    pmccntr_init();
    t0 = pmccntr_read();
    for (uint32_t i = 0; i < iters; i++) membw_memset(addr, size);
    t1 = pmccntr_read();
    uint32_t memset_ticks = t1 - t0;

    pmccntr_init();
    t0 = pmccntr_read();
    for (uint32_t i = 0; i < iters; i++) membw_read(addr, size);
    t1 = pmccntr_read();
    uint32_t read_ticks = t1 - t0;

    pmccntr_init();
    t0 = pmccntr_read();
    for (uint32_t i = 0; i < iters; i++) membw_memcpy(addr + size, addr, size);
    t1 = pmccntr_read();
    uint32_t memcpy_ticks = t1 - t0;

    uint8_t resp[32];
    write_le32(&resp[0],  addr);
    write_le32(&resp[4],  size);
    write_le32(&resp[8],  iters);
    write_le32(&resp[12], timer_hz);
    write_le32(&resp[16], memset_ticks);
    write_le32(&resp[20], read_ticks);
    write_le32(&resp[24], memcpy_ticks);
    write_le32(&resp[28], 1);   /* cpu_arch: 1 = ARMv7 Cortex-A */
    proto_send(RSP_MEMBW, resp, sizeof(resp));
#endif
}

/* Forward declaration */
static void handle_flash_write(const uint8_t *data, uint32_t len);

/*
 * CMD_WRITE: receive data and write to RAM or flash.
 *
 * If addr < flash_size, routes to flash write (erase assumed done).
 * Otherwise, writes to RAM.
 */
static void handle_write(const uint8_t *data, uint32_t len) {
    if (len < 12) { proto_send_ack(ACK_CRC_ERROR); return; }

    uint32_t addr = read_le32(&data[0]);
    uint32_t size = read_le32(&data[4]);

    /* Route flash addresses to flash write handler */
    if (flash_readable && addr < flash_info.size &&
        (addr + size) <= flash_info.size) {
        handle_flash_write(data, len);
        return;
    }

    uint32_t expected_crc = read_le32(&data[8]);

    /* Validate: must be in writable RAM, reasonable size */
    if (size == 0 || size > MAX_READ_SIZE ||
        addr < RAM_BASE || (addr + size) <= addr) {
        proto_send_ack(ACK_FLASH_ERROR);
        return;
    }

    uint8_t *dest = (uint8_t *)addr;
    proto_send_ack(ACK_OK);  /* Ready to receive */

    uint32_t received = 0;
    uint8_t pkt[MAX_PAYLOAD + 16];
    while (received < size) {
        uint32_t pkt_len = 0;
        uint8_t cmd = proto_recv(pkt, &pkt_len, 10000);
        if (cmd == RSP_DATA && pkt_len > 2) {
            uint32_t chunk = pkt_len - 2;
            for (uint32_t i = 0; i < chunk && received < size; i++)
                dest[received++] = pkt[2 + i];
            /* No per-packet ACK — streaming mode. COBS CRC32 per packet
             * catches any corruption. D-cache makes processing fast
             * enough to keep up with 921600 baud. */
        } else if (cmd == 0) {
            uint8_t err[5];
            err[0] = ACK_FLASH_ERROR;
            write_le32(&err[1], received);
            proto_send(RSP_ACK, err, 5);
            return;
        }
    }

    /* Verify CRC32 */
    uint32_t actual_crc = crc32(0, dest, size);
    if (actual_crc != expected_crc) {
        uint8_t err[9];
        err[0] = ACK_CRC_ERROR;
        write_le32(&err[1], actual_crc);
        write_le32(&err[5], received);
        proto_send(RSP_ACK, err, 9);
        return;
    }

    proto_send_ack(ACK_OK);
}

/*
 * CMD_ERASE: erase flash sectors.
 *   Host sends: CMD_ERASE [addr:4LE] [size:4LE]
 *   Agent erases sectors covering the range.
 *   Sends RSP_DATA with [sectors_done:2LE] after each sector for progress.
 *   Final ACK_OK when all sectors erased.
 */
static void handle_erase(const uint8_t *data, uint32_t len) {
    if (len < 8) { proto_send_ack(ACK_CRC_ERROR); return; }
    if (!flash_readable) { proto_send_ack(ACK_FLASH_ERROR); return; }

    uint32_t addr = read_le32(&data[0]);
    uint32_t size = read_le32(&data[4]);
    uint32_t sector_sz = flash_info.sector_size;

    /* Validate: must be within flash, sector-aligned */
    if (size == 0 || addr + size > flash_info.size ||
        (addr % sector_sz) != 0 || (size % sector_sz) != 0) {
        proto_send_ack(ACK_FLASH_ERROR);
        return;
    }

    uint32_t num_sectors = size / sector_sz;

    /* Send ACK to start erasing */
    proto_send_ack(ACK_OK);

    for (uint32_t i = 0; i < num_sectors; i++) {
        if (flash_erase_sector(addr + i * sector_sz) != 0) {
            proto_send_ack(ACK_FLASH_ERROR);
            return;
        }

        /* Progress: [sectors_done:2LE][debug:3B] */
        uint8_t progress[5];
        progress[0] = ((i + 1) >> 0) & 0xFF;
        progress[1] = ((i + 1) >> 8) & 0xFF;
        progress[2] = flash_unlock_debug[0];  /* SR before unlock */
        progress[3] = flash_unlock_debug[1];  /* SR after write_enable */
        progress[4] = flash_unlock_debug[2];  /* SR after unlock */
        proto_send(RSP_DATA, progress, 5);
    }

    proto_send_ack(ACK_OK);
}

/*
 * CMD_FLASH_WRITE: receive data and program it to flash.
 *   Host sends: CMD_WRITE [flash_addr:4LE] [size:4LE] [expected_crc:4LE]
 *   with flash_addr in flash range (0 to flash_size).
 *
 *   Agent receives data into RAM staging area, verifies CRC,
 *   then programs flash page-by-page. Assumes sectors already erased.
 *
 *   Sends ACK_OK after all pages written + verified.
 */
static void handle_flash_write(const uint8_t *data, uint32_t len) {
    if (len < 12) { proto_send_ack(ACK_CRC_ERROR); return; }
    if (!flash_readable) { proto_send_ack(ACK_FLASH_ERROR); return; }

    uint32_t flash_addr = read_le32(&data[0]);
    uint32_t size = read_le32(&data[4]);
    uint32_t expected_crc = read_le32(&data[8]);

    if (size == 0 || size > flash_info.size ||
        flash_addr + size > flash_info.size) {
        proto_send_ack(ACK_FLASH_ERROR);
        return;
    }

    /* Receive data into RAM staging area */
    uint8_t *staging = (uint8_t *)(RAM_BASE + 0x200000);
    proto_send_ack(ACK_OK);

    uint32_t received = 0;
    uint8_t pkt[MAX_PAYLOAD + 16];
    while (received < size) {
        uint32_t pkt_len = 0;
        uint8_t cmd = proto_recv(pkt, &pkt_len, 10000);
        if (cmd == RSP_DATA && pkt_len > 2) {
            uint32_t chunk = pkt_len - 2;
            for (uint32_t i = 0; i < chunk && received < size; i++)
                staging[received++] = pkt[2 + i];
        } else if (cmd == 0) {
            uint8_t err[5];
            err[0] = ACK_FLASH_ERROR;
            write_le32(&err[1], received);
            proto_send(RSP_ACK, err, 5);
            return;
        }
    }

    /* Verify received data CRC */
    uint32_t actual_crc = crc32(0, staging, size);
    if (actual_crc != expected_crc) {
        uint8_t err[9];
        err[0] = ACK_CRC_ERROR;
        write_le32(&err[1], actual_crc);
        write_le32(&err[5], received);
        proto_send(RSP_ACK, err, 9);
        return;
    }

    /* Program flash page-by-page */
    uint32_t page_sz = flash_info.page_size;
    uint32_t offset = 0;
    while (offset < size) {
        uint32_t chunk = size - offset;
        if (chunk > page_sz) chunk = page_sz;
        flash_write_page(flash_addr + offset, &staging[offset], chunk);
        offset += chunk;
    }

    /* Verify written data by reading back from flash and comparing CRC */
    const uint8_t *flash_ptr = (const uint8_t *)(FLASH_MEM + flash_addr);
    uint32_t verify_crc = crc32(0, flash_ptr, size);
    if (verify_crc != expected_crc) {
        uint8_t err[9];
        err[0] = ACK_CRC_ERROR;
        write_le32(&err[1], verify_crc);
        write_le32(&err[5], size);
        proto_send(RSP_ACK, err, 9);
        return;
    }

    proto_send_ack(ACK_OK);
}

/*
 * CMD_FLASH_PROGRAM: erase + program flash from RAM.
 *
 * The U-Boot approach: host writes data to RAM first (via CMD_WRITE),
 * then sends this command to erase + program flash from that RAM buffer.
 * Agent does the entire flash operation locally, sending per-sector
 * progress so the host knows it's alive.
 *
 *   Host sends: CMD_FLASH_PROGRAM [ram_addr:4LE] [flash_addr:4LE]
 *               [size:4LE] [expected_crc:4LE]
 *   Agent: verifies RAM CRC → erases sectors → programs pages
 *   Progress: RSP_DATA [sectors_done:2LE] [total_sectors:2LE] per sector
 *   Final: ACK_OK or ACK_CRC_ERROR/ACK_FLASH_ERROR
 */
static void handle_flash_program(const uint8_t *data, uint32_t len) {
    if (len < 16) { proto_send_ack(ACK_CRC_ERROR); return; }
    if (!flash_readable) { proto_send_ack(ACK_FLASH_ERROR); return; }

    uint32_t ram_addr = read_le32(&data[0]);
    uint32_t flash_addr = read_le32(&data[4]);
    uint32_t size = read_le32(&data[8]);
    uint32_t expected_crc = read_le32(&data[12]);

    /* Validate */
    if (size == 0 || flash_addr + size > flash_info.size) {
        proto_send_ack(ACK_FLASH_ERROR);
        return;
    }
    if (!addr_readable(ram_addr, size)) {
        proto_send_ack(ACK_FLASH_ERROR);
        return;
    }

    const uint8_t *src = (const uint8_t *)ram_addr;

    /* Verify RAM data CRC before touching flash */
    uint32_t actual_crc = crc32(0, src, size);
    if (actual_crc != expected_crc) {
        uint8_t err[9];
        err[0] = ACK_CRC_ERROR;
        write_le32(&err[1], actual_crc);
        write_le32(&err[5], size);
        proto_send(RSP_ACK, err, 9);
        return;
    }

    proto_send_ack(ACK_OK);  /* CRC verified, starting flash operation */

    uint32_t sector_sz = flash_info.sector_size;
    uint32_t page_sz = flash_info.page_size;

    /* Round up to sector boundary for erase */
    uint32_t erase_start = flash_addr & ~(sector_sz - 1);
    uint32_t erase_end = (flash_addr + size + sector_sz - 1) & ~(sector_sz - 1);
    uint32_t num_sectors = (erase_end - erase_start) / sector_sz;
    uint32_t total_sectors = num_sectors;

    /* Phase 1: Erase sectors */
    for (uint32_t s = 0; s < num_sectors; s++) {
        if (flash_erase_sector(erase_start + s * sector_sz) != 0) {
            proto_send_ack(ACK_FLASH_ERROR);
            return;
        }

        /* Progress: [sectors_done:2LE] [total:2LE] */
        uint8_t progress[4];
        progress[0] = ((s + 1) >> 0) & 0xFF;
        progress[1] = ((s + 1) >> 8) & 0xFF;
        progress[2] = (total_sectors >> 0) & 0xFF;
        progress[3] = (total_sectors >> 8) & 0xFF;
        proto_send(RSP_DATA, progress, 4);
    }

    /* Phase 2: Program pages */
    uint32_t offset = 0;
    while (offset < size) {
        uint32_t chunk = size - offset;
        if (chunk > page_sz) chunk = page_sz;
        flash_write_page(flash_addr + offset, &src[offset], chunk);
        offset += chunk;

        /* Progress every 64 pages (16KB) to keep host alive */
        if ((offset % (page_sz * 64)) == 0 || offset >= size) {
            uint8_t progress[4];
            uint16_t done = (uint16_t)(total_sectors + offset / (page_sz * 64));
            uint16_t total = (uint16_t)(total_sectors + size / (page_sz * 64));
            progress[0] = (done >> 0) & 0xFF;
            progress[1] = (done >> 8) & 0xFF;
            progress[2] = (total >> 0) & 0xFF;
            progress[3] = (total >> 8) & 0xFF;
            proto_send(RSP_DATA, progress, 4);
        }
    }

    /* Skip in-agent verify — the FMC memory window may have stale data
     * after bulk programming (65536 soft resets). Host verifies separately. */
    proto_send_ack(ACK_OK);
}

/*
 * CMD_FLASH_STREAM: stream data from UART directly to flash.
 *
 * Processes one sector at a time: erase → receive → program.
 * No separate RAM upload phase — data flows UART → RAM buffer → flash.
 * Host streams DATA packets continuously; agent sends per-sector progress.
 *
 *   Host sends: CMD_FLASH_STREAM [flash_addr:4LE] [size:4LE] [crc:4LE]
 *   Agent ACKs, then for each sector: erase, receive 64KB, program pages.
 *   Progress: RSP_DATA [sector_done:2LE] [total:2LE] after each sector.
 *   Final: ACK_OK or ACK_CRC_ERROR.
 */
/* Check if a 256-byte page is all 0xFF (erased state).
 * Uses word-aligned reads for speed (~0.1µs vs 2ms page program). */
static int page_is_ff(const uint8_t *data, uint32_t len) {
    const uint32_t *w = (const uint32_t *)data;
    for (uint32_t i = 0; i < len / 4; i++)
        if (w[i] != 0xFFFFFFFF) return 0;
    return 1;
}

/* Erase sector + program non-0xFF pages from buffer.
 * Returns 0 on success, -1 if erase failed the post-erase verify. */
static int erase_and_program(uint32_t addr, const uint8_t *buf,
                              uint32_t bytes, uint32_t page_sz,
                              int drain_fifo) {
    if (flash_erase_sector(addr) != 0) return -1;
    uint32_t offset = 0;
    while (offset < bytes) {
        uint32_t chunk = bytes - offset;
        if (chunk > page_sz) chunk = page_sz;
        if (!page_is_ff(&buf[offset], chunk))
            flash_write_page(addr + offset, &buf[offset], chunk);
        offset += chunk;
        if (drain_fifo) proto_drain_fifo();
    }
    return 0;
}

static void handle_flash_stream(const uint8_t *data, uint32_t len) {
    if (len < 44) { proto_send_ack(ACK_CRC_ERROR); return; }
    if (!flash_readable) { proto_send_ack(ACK_FLASH_ERROR); return; }

    uint32_t flash_addr = read_le32(&data[0]);
    uint32_t size = read_le32(&data[4]);
    uint32_t expected_crc = read_le32(&data[8]);
    const uint8_t *bitmap = &data[12];  /* 32-byte sector bitmap */

    if (size == 0 || flash_addr + size > flash_info.size) {
        proto_send_ack(ACK_FLASH_ERROR);
        return;
    }

    uint32_t sector_sz = flash_info.sector_size;
    uint32_t page_sz = flash_info.page_size;
    uint32_t num_sectors = (size + sector_sz - 1) / sector_sz;

    /* Double buffer: receive into one while erasing+programming the other.
     * Each buffer must be at least sector_sz bytes — NAND sectors are
     * 128 KiB, NOR up to 64 KiB. Spacing by 128 KiB covers both. Older
     * 64 KiB spacing caused sector N+1's receive to clobber the second
     * half of sector N during NAND program (bug observed on av200). */
    uint8_t *buf[2] = {
        (uint8_t *)(RAM_BASE + 0x200000),
        (uint8_t *)(RAM_BASE + 0x220000),
    };
    int rx_buf = 0;  /* Buffer currently being filled */

    proto_send_ack(ACK_OK);

    uint32_t total_received = 0;
    /* Pending sector to erase+program (from previous iteration) */
    int pending_buf = -1;
    uint32_t pending_addr = 0;
    uint32_t pending_bytes = 0;

    for (uint32_t s = 0; s < num_sectors; s++) {
        uint32_t sector_offset = s * sector_sz;
        uint32_t sector_bytes = size - sector_offset;
        if (sector_bytes > sector_sz) sector_bytes = sector_sz;

        /* Check bitmap: bit=0 means sector is all 0xFF, skip it */
        if (!(bitmap[s / 8] & (1 << (s % 8)))) {
            /* Still process pending data sector if any */
            if (pending_buf >= 0) {
                if (erase_and_program(pending_addr, buf[pending_buf],
                                       pending_bytes, page_sz, 1) != 0) {
                    proto_send_ack(ACK_FLASH_ERROR);
                    return;
                }
                pending_buf = -1;
            }

            /* Erase this sector (ensure 0xFF state) but don't program */
            if (flash_erase_sector(flash_addr + sector_offset) != 0) {
                proto_send_ack(ACK_FLASH_ERROR);
                return;
            }

            /* Send progress — no data to receive */
            uint8_t progress[4];
            progress[0] = ((s + 1) >> 0) & 0xFF;
            progress[1] = ((s + 1) >> 8) & 0xFF;
            progress[2] = (num_sectors >> 0) & 0xFF;
            progress[3] = (num_sectors >> 8) & 0xFF;
            proto_send(RSP_DATA, progress, 4);

            continue;  /* Don't touch rx_buf or set new pending */
        }

        /* Receive sector data into rx_buf.
         * No flash operations during receive — UART stays responsive. */
        uint32_t buf_received = 0;
        uint8_t pkt[MAX_PAYLOAD + 16];
        while (buf_received < sector_bytes) {
            uint32_t pkt_len = 0;
            uint8_t cmd = proto_recv(pkt, &pkt_len, 10000);
            if (cmd == RSP_DATA && pkt_len > 2) {
                uint32_t chunk = pkt_len - 2;
                for (uint32_t i = 0; i < chunk && buf_received < sector_bytes; i++)
                    buf[rx_buf][buf_received++] = pkt[2 + i];
            } else if (cmd == 0) {
                uint8_t err[5];
                err[0] = ACK_FLASH_ERROR;
                write_le32(&err[1], total_received + buf_received);
                proto_send(RSP_ACK, err, 5);
                return;
            }
        }

        total_received += sector_bytes;

        /* Tell host: "sector received, send next now!"
         * Host starts streaming next sector immediately. */
        {
            uint8_t progress[4];
            progress[0] = ((s + 1) >> 0) & 0xFF;
            progress[1] = ((s + 1) >> 8) & 0xFF;
            progress[2] = (num_sectors >> 0) & 0xFF;
            progress[3] = (num_sectors >> 8) & 0xFF;
            proto_send(RSP_DATA, progress, 4);
        }

        /* Process previous sector if pending (erase + program).
         * Host is streaming next sector into the OTHER buffer right now. */
        if (pending_buf >= 0) {
            if (erase_and_program(pending_addr, buf[pending_buf],
                                   pending_bytes, page_sz, 1) != 0) {
                proto_send_ack(ACK_FLASH_ERROR);
                return;
            }
        }

        /* This sector's buffer becomes the pending one */
        pending_buf = rx_buf;
        pending_addr = flash_addr + sector_offset;
        pending_bytes = sector_bytes;

        /* Swap to the other buffer for next receive */
        rx_buf ^= 1;
    }

    /* Process the last sector (no more data to receive) */
    if (pending_buf >= 0) {
        if (erase_and_program(pending_addr, buf[pending_buf],
                               pending_bytes, page_sz, 0) != 0) {
            proto_send_ack(ACK_FLASH_ERROR);
            return;
        }
    }

    proto_send_ack(ACK_OK);
}

/*
 * ARM32 position-independent trampoline (machine code).
 * Copies r2 bytes from r1 to r0, then branches to r0 (original dst).
 *
 * ARMv7-A variant additionally cleans D-cache and invalidates I-cache
 * over the destination range before BX so the I-fetch unit pulls the
 * just-written bytes from memory instead of stale lines from when the
 * old agent occupied that address.  Without this, selfupdate either
 * runs with stale instructions (silent crash, no UART) or — when both
 * binaries happen to be byte-identical at the cached lines — works by
 * coincidence.
 *
 * ARM926 (ARMv5TEJ) keeps the original short trampoline; if/when we
 * actually exercise selfupdate on ARM926 boards we'll need the same
 * treatment with V5 cache-op encodings.
 */
#ifdef CPU_ARM926
static const uint32_t trampoline_arm[] = {
    0xe1a03000,  /* mov  r3, r0       ; save dst  */
    0xe3520000,  /* cmp  r2, #0                    */
    0x0a000003,  /* beq  done                      */
    /* loop: */
    0xe4d14001,  /* ldrb r4, [r1], #1              */
    0xe4c04001,  /* strb r4, [r0], #1              */
    0xe2522001,  /* subs r2, r2, #1                */
    0x1afffffb,  /* bne  loop                      */
    /* done: */
    0xe12fff13,  /* bx   r3           ; jump to dst */
};
#else
/* ARMv7-A: copy + per-line DCCMVAU + ICIMVAU + DSB + ISB + BX.
 * Source: agent/tramp_v7.S, assembled with arm-none-eabi-gcc -mcpu=cortex-a7.
 * Walks the destination in 32-byte cache-line steps (smallest line size
 * among supported ARMv7-A cores; over-walking is harmless). */
static const uint32_t trampoline_arm[] = {
    0xe1a03000,  /* mov  r3, r0           ; save dst                       */
    0xe1a05002,  /* mov  r5, r2           ; save size                      */
    0xe3520000,  /* cmp  r2, #0                                             */
    0x0a000003,  /* beq  done                                               */
    /* copy_loop: */
    0xe4d14001,  /* ldrb r4, [r1], #1                                       */
    0xe4c04001,  /* strb r4, [r0], #1                                       */
    0xe2522001,  /* subs r2, r2, #1                                         */
    0x1afffffb,  /* bne  copy_loop                                          */
    /* done: */
    0xe1a00003,  /* mov  r0, r3           ; restore dst start              */
    0xe0836005,  /* add  r6, r3, r5       ; end = dst + size               */
    0xe3c0001f,  /* bic  r0, r0, #31      ; round down to cache line       */
    /* cache_loop: */
    0xee070f3b,  /* mcr  p15, 0, r0, c7, c11, 1   ; DCCMVAU                */
    0xee070f35,  /* mcr  p15, 0, r0, c7, c5, 1    ; ICIMVAU                */
    0xe2800020,  /* add  r0, r0, #32                                        */
    0xe1500006,  /* cmp  r0, r6                                             */
    0xbafffffa,  /* blt  cache_loop                                         */
    0xf57ff04f,  /* dsb  sy                                                 */
    0xf57ff06f,  /* isb  sy                                                 */
    0xe12fff13,  /* bx   r3                                                 */
};
#endif

/*
 * CMD_SELFUPDATE: receive new agent binary, verify, copy and jump.
 *
 * Receives data to a staging area (1MB above load addr) to avoid
 * overwriting the running code. After CRC verification, copies a
 * small trampoline to the stack, which then copies staging → load
 * addr and jumps to the new code.
 */
static void handle_selfupdate(const uint8_t *data, uint32_t len) {
    if (len < 12) { proto_send_ack(ACK_CRC_ERROR); return; }

    uint32_t addr = read_le32(&data[0]);
    uint32_t size = read_le32(&data[4]);
    uint32_t expected_crc = read_le32(&data[8]);

    if (size == 0 || size > MAX_UPDATE_SIZE ||
        addr < RAM_BASE || (addr + size) <= addr) {
        proto_send_ack(ACK_FLASH_ERROR);
        return;
    }

    /* Stage 1MB above target to avoid overwriting running code */
    uint32_t staging = addr + 0x100000;
    uint8_t *dest = (uint8_t *)staging;

    proto_send_ack(ACK_OK);

    uint32_t received = 0;
    uint8_t pkt[MAX_PAYLOAD + 16];
    while (received < size) {
        uint32_t pkt_len = 0;
        uint8_t cmd = proto_recv(pkt, &pkt_len, 10000);
        if (cmd == RSP_DATA && pkt_len > 2) {
            uint32_t chunk = pkt_len - 2;
            for (uint32_t i = 0; i < chunk && received < size; i++)
                dest[received++] = pkt[2 + i];
            /* Streaming mode — no per-packet ACK */
        } else if (cmd == 0) {
            proto_send_ack(ACK_FLASH_ERROR);
            return;
        }
    }

    uint32_t actual_crc = crc32(0, dest, size);
    if (actual_crc != expected_crc) {
        proto_send_ack(ACK_CRC_ERROR);
        return;  /* Stay alive — don't jump to bad code */
    }

    proto_send_ack(ACK_OK);

    /* Flush UART TX */
    for (volatile int i = 0; i < 100000; i++) {}

    /* Copy trampoline to a fixed safe RAM location (RAM_BASE + 0x200)
     * well below the agent code and stack. Then execute it. */
    volatile uint32_t *tramp_dst = (volatile uint32_t *)(RAM_BASE + 0x200);
    for (uint32_t i = 0; i < sizeof(trampoline_arm) / sizeof(uint32_t); i++)
        tramp_dst[i] = trampoline_arm[i];

    /* CRITICAL: writes above land in D-cache. Without explicit cache
     * maintenance the CPU's I-fetch at RAM_BASE+0x200 reads stale memory
     * contents (whatever was there before — typically zeros) instead of
     * the trampoline bytes, and execution branches into garbage.
     * Clean D-cache for the trampoline range to push writes to memory,
     * then invalidate the entire I-cache so new fetches hit memory. */
#ifndef CPU_ARM926
    {
        uintptr_t start = (uintptr_t)tramp_dst & ~31u;
        uintptr_t end = (uintptr_t)tramp_dst + sizeof(trampoline_arm);
        for (uintptr_t a = start; a < end; a += 32) {
            asm volatile("mcr p15, 0, %0, c7, c11, 1" :: "r"(a) : "memory");
        }
        asm volatile("dsb" ::: "memory");
        asm volatile("mcr p15, 0, %0, c7, c5, 0" :: "r"(0) : "memory");  /* ICIALLU */
        asm volatile("mcr p15, 0, %0, c7, c5, 6" :: "r"(0) : "memory");  /* BPIALL */
        asm volatile("dsb" ::: "memory");
        asm volatile("isb" ::: "memory");
    }
#endif

    void (*tramp)(uint32_t, uint32_t, uint32_t) =
        (void (*)(uint32_t, uint32_t, uint32_t))(void *)(RAM_BASE + 0x200);
    tramp(addr, staging, size);
    /* Never returns */
}

/*
 * CMD_SCAN: scan flash health sector-by-sector.
 *
 * For each sector, computes CRC32 and checks for patterns (all-FF, all-same).
 * Does a second CRC32 pass to detect unstable (degrading) sectors.
 * Returns compact results: [status:1B][crc32:4B] per sector.
 *
 * Sector status:
 *   0x00 = GOOD (non-trivial data, CRC stable)
 *   0x01 = EMPTY (all 0xFF)
 *   0x02 = STUCK_ZERO (all 0x00)
 *   0x03 = STUCK_PATTERN (all same byte, not FF/00)
 *   0x04 = UNSTABLE (CRC differs between two reads)
 *   0x05 = READ_ERROR
 */
#define SCAN_GOOD         0x00
#define SCAN_EMPTY        0x01
#define SCAN_STUCK_ZERO   0x02
#define SCAN_STUCK_PAT    0x03
#define SCAN_UNSTABLE     0x04
#define SCAN_READ_ERROR   0x05
#define SCAN_BAD_BLOCK    0x06   /* NAND only: factory-marked bad block (OOB[0] != 0xFF) */

#define SCAN_BATCH_MAX    8  /* sectors per RSP_SCAN packet — small for live progress */

/* CMD_MARK_BAD: write 0x00 to OOB[0] of page 0 of a NAND block, marking
 * it as a factory-style bad block.  Used for testing the scan's bad-block
 * detection (synthesize a bad block, scan, then erase to clear).
 *   Host sends: CMD_MARK_BAD [block:4LE]
 *   Reply: ACK_OK or ACK_FLASH_ERROR */
static void handle_mark_bad(const uint8_t *data, uint32_t len) {
    if (len < 4) { proto_send_ack(ACK_CRC_ERROR); return; }
    if (!flash_readable) { proto_send_ack(ACK_FLASH_ERROR); return; }
    if (flash_info.flash_type != FLASH_TYPE_NAND) {
        proto_send_ack(ACK_FLASH_ERROR); return;  /* NOR has no OOB */
    }
    uint32_t block = read_le32(&data[0]);
    uint32_t num_blocks = flash_info.size / flash_info.sector_size;
    if (block >= num_blocks) { proto_send_ack(ACK_FLASH_ERROR); return; }

    uint8_t marker = 0x00;
    int rc = flash_program_oob(block, &marker, 1);
    proto_send_ack(rc == 0 ? ACK_OK : ACK_FLASH_ERROR);
}

static void handle_scan(const uint8_t *data __attribute__((unused)),
                        uint32_t len __attribute__((unused))) {
    if (!flash_readable) {
        proto_send_ack(ACK_FLASH_ERROR);
        return;
    }

    uint32_t sector_sz = flash_info.sector_size;
    if (sector_sz == 0) sector_sz = 0x10000;
    uint32_t num_sectors = flash_info.size / sector_sz;
    if (num_sectors == 0) {
        proto_send_ack(ACK_FLASH_ERROR);
        return;
    }

    /* Send header: sector count */
    uint8_t header[4];
    write_le32(header, num_sectors);
    proto_send(RSP_SCAN, header, 4);

    /* Process sectors in batches.  scan_buf holds one sector's worth of
     * bytes for NAND (which can't be read via mem-mapped pointer); on NOR
     * we still use the direct pointer for zero-copy.  Sized for the
     * largest NAND block we currently support (128 KiB on MX35LF). */
    static uint8_t scan_buf[128 * 1024];
    uint8_t result_buf[SCAN_BATCH_MAX * 5];
    uint32_t buf_idx = 0;
    int is_nand = (flash_info.flash_type == FLASH_TYPE_NAND);

    for (uint32_t s = 0; s < num_sectors; s++) {
        uint8_t status = SCAN_GOOD;
        const uint8_t *ptr;

        proto_drain_fifo();

        if (is_nand) {
            /* NAND: factory bad-block marker is OOB[0] of page 0 of the
             * block.  If != 0xFF the block was marked bad at the factory
             * (or by previous wear) and must not be erased/programmed. */
            uint8_t oob[2];
            if (flash_read_oob(s, oob, 2) == 0 && oob[0] != 0xFF) {
                /* Bad block — skip data-area scan, report and continue. */
                result_buf[buf_idx++] = SCAN_BAD_BLOCK;
                write_le32(&result_buf[buf_idx], 0);
                buf_idx += 4;
                if (buf_idx >= SCAN_BATCH_MAX * 5 || s == num_sectors - 1) {
                    proto_send(RSP_SCAN, result_buf, buf_idx);
                    buf_idx = 0;
                }
                continue;
            }
            /* Good block: read data area into RAM buf for the rest of the
             * scan (memory-mapped reads don't work on NAND). */
            flash_read(s * sector_sz, scan_buf, sector_sz);
            ptr = scan_buf;
        } else {
            /* NOR: direct mem-mapped read (FMC boot-mode window). */
            ptr = (const uint8_t *)(FLASH_MEM + s * sector_sz);
        }

        /* Pass 1: CRC32 */
        uint32_t crc1 = crc32(0, ptr, sector_sz);

        proto_drain_fifo();

        /* Pattern check: sample first byte, scan for uniformity */
        uint8_t first = ptr[0];
        int all_ff = (first == 0xFF);
        int all_same = 1;

        for (uint32_t i = 1; i < sector_sz; i++) {
            uint8_t b = ptr[i];
            if (b != 0xFF) all_ff = 0;
            if (b != first) all_same = 0;
            if (!all_ff && !all_same) break;
        }

        if (all_ff) {
            status = SCAN_EMPTY;
        } else if (all_same && first == 0x00) {
            status = SCAN_STUCK_ZERO;
        } else if (all_same) {
            status = SCAN_STUCK_PAT;
        } else if (!is_nand) {
            /* Pass 2: stability check — re-read CRC.  NOR only because
             * NAND re-reads always go through the same on-chip ECC and
             * would never report differences (the chip auto-corrects
             * single-bit flips).  For NAND, ECC error would be a
             * separate signal we don't yet expose. */
            proto_drain_fifo();
            uint32_t crc2 = crc32(0, ptr, sector_sz);
            if (crc2 != crc1) status = SCAN_UNSTABLE;
        }

        /* Pack result: [status:1][crc32:4] */
        result_buf[buf_idx++] = status;
        write_le32(&result_buf[buf_idx], crc1);
        buf_idx += 4;

        proto_drain_fifo();

        /* Flush batch when full or last sector */
        if (buf_idx >= SCAN_BATCH_MAX * 5 || s == num_sectors - 1) {
            proto_send(RSP_SCAN, result_buf, buf_idx);
            buf_idx = 0;
        }
    }

    proto_send_ack(ACK_OK);
}

/*
 * CMD_SET_BAUD: change UART baud rate.
 *   Host sends: CMD_SET_BAUD [baud_rate:4LE]
 *   Agent ACKs at current baud, waits for TX to drain, switches.
 *   Host should switch immediately after receiving the ACK.
 */
static int at_default_baud = 1;

static void handle_set_baud(const uint8_t *data, uint32_t len) {
    if (len < 4) { proto_send_ack(ACK_CRC_ERROR); return; }

    uint32_t baud = read_le32(&data[0]);

    /* Sanity check: reject absurd baud rates */
    if (baud < 9600 || baud > 3000000) {
        proto_send_ack(ACK_FLASH_ERROR);
        return;
    }

    /* ACK at current baud rate so host knows we accepted */
    proto_send_ack(ACK_OK);

    /* Switch to new baud (uart_set_baud waits for TX drain) */
    uart_set_baud(baud);

    /* Drain any garbage from baud rate transition */
    while (uart_readable()) uart_getc();

    /* Stay at the new baud unconditionally. Earlier versions waited up
     * to 3 s for a verification packet and reverted to 115200 otherwise,
     * but proto_recv's "3 s" deadline is a CPU-speed-dependent busy-wait
     * (≈25-cycle loop × 100·timeout_ms iterations) — on a fast Cortex-A7
     * the actual window collapses to <300 ms, which is shorter than the
     * host-side WiFi-RTT for the rack pod's `POST /uart/baud` (≈1 s).
     * The agent reverted before the host's verification packet could
     * arrive at the new rate, leaving host/agent permanently mismatched
     * and reading misclocked garbage.
     *
     * Failure mode if the host can't reach us at the new baud: agent is
     * unrecoverable until the next power-cycle / fastboot, which the
     * rack pod or RouterOS can both do trivially. */
    at_default_baud = (baud == 115200);
}

int main(void) {
    watchdog_disable();
    uart_init();

    /* Drain any stale bytes left in UART RX FIFO from boot protocol */
    while (uart_readable()) uart_getc();

    /* Initialize flash controller — enables memory-mapped reads */
    if (flash_init(&flash_info) == 0) {
        flash_readable = 1;
    }

#ifdef EMMC_BASE
    /* If no SPI flash was identified (JEDEC ID came back 0/0xFF/0xFF or the
     * FMC controller wasn't on the expected version), try the eMMC reader.
     * Read-only MVP: identifies the card and exposes a linear LBA window
     * through the FLASH_MEM virtual address so CMD_READ can dump blocks. */
    if (!flash_readable && emmc_init() == 0) {
        flash_info.jedec_id[0] = emmc_cid[0];   /* MID */
        flash_info.jedec_id[1] = emmc_cid[1];
        flash_info.jedec_id[2] = emmc_cid[2];
        flash_info.size        = (uint32_t)(emmc_capacity_bytes > 0xFFFFFFFFu
                                            ? 0xFFFFFFFFu
                                            : emmc_capacity_bytes);
        flash_info.sector_size = 512;
        flash_info.page_size   = 512;
        flash_info.flash_type  = FLASH_TYPE_EMMC;
        flash_readable = 1;
    }
#endif

    proto_send_ready();

    uint32_t idle_count = 0;
    uint32_t baud_idle = 0;
    at_default_baud = 1;
    while (1) {
        uint32_t data_len = 0;
        uint8_t cmd = proto_recv(cmd_buf, &data_len, 500);

        if (cmd == 0) {
            idle_count++;
            if (idle_count >= 4) {
                proto_send_ready();
                idle_count = 0;
            }
            /* If at non-default baud and idle for ~10s (20 x 500ms),
             * revert to 115200. Host may have disconnected. */
            if (!at_default_baud) {
                baud_idle++;
                if (baud_idle >= 60) {  /* ~30 seconds */
                    uart_set_baud(115200);
                    while (uart_readable()) uart_getc();
                    at_default_baud = 1;
                    baud_idle = 0;
                }
            }
            continue;
        }
        idle_count = 0;
        baud_idle = 0;

        switch (cmd) {
            case CMD_INFO:
                handle_info();
                break;
            case CMD_READ:
                handle_read(cmd_buf, data_len);
                break;
            case CMD_WRITE:
                handle_write(cmd_buf, data_len);
                break;
            case CMD_ERASE:
                handle_erase(cmd_buf, data_len);
                break;
            case CMD_CRC32:
                handle_crc32_cmd(cmd_buf, data_len);
                break;
            case CMD_SELFUPDATE:
                handle_selfupdate(cmd_buf, data_len);
                break;
            case CMD_SCAN:
                handle_scan(cmd_buf, data_len);
                break;
            case CMD_FLASH_PROGRAM:
                handle_flash_program(cmd_buf, data_len);
                break;
            case CMD_FLASH_STREAM:
                handle_flash_stream(cmd_buf, data_len);
                break;
            case CMD_MARK_BAD:
                handle_mark_bad(cmd_buf, data_len);
                break;
            case CMD_MEMBW:
                handle_membw(cmd_buf, data_len);
                break;
            case CMD_SET_BAUD:
                handle_set_baud(cmd_buf, data_len);
                break;
            case CMD_REBOOT:
                /* ACK first, then system reset via sysctrl register.
                 * This is the standard HiSilicon reset (same as Linux
                 * hisi-reboot driver): write 0xdeadbeef to SYSCTRL_REBOOT.
                 * Address is per-SoC (V3/V4 generations differ). */
                proto_send_ack(ACK_OK);
                for (volatile int i = 0; i < 100000; i++) {}
                *(volatile uint32_t *)SYSCTRL_REBOOT = 0xdeadbeef;
                while (1) {}
                break;
            default:
                proto_send_ack(ACK_CRC_ERROR);
                break;
        }
    }
}
