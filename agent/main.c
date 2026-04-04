/*
 * defib flash agent — bare-metal main loop.
 * Receives commands from host via COBS-framed UART protocol.
 */

#include <stdint.h>
#include "uart.h"
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
    return p[0] | (p[1] << 8) | (p[2] << 16) | (p[3] << 24);
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
    /* I/O register regions (CRG, FMC controller, system controller) */
    if (addr >= 0x10000000 && (addr + size) <= 0x10001000) return 1; /* FMC regs */
    if (addr >= 0x12010000 && (addr + size) <= 0x12020000) return 1; /* CRG */
    if (addr >= 0x12020000 && (addr + size) <= 0x12030000) return 1; /* SYS_CTRL */
    /* Flash memory-mapped window — only after flash_init succeeds */
    if (flash_readable && addr >= FLASH_MEM && (addr + size) <= (FLASH_MEM + 32 * 1024 * 1024))
        return 1;
    return 0;
}

static void handle_info(void) {
    uint8_t resp[16];
    /* JEDEC ID in first 4 bytes (3 bytes + padding) */
    resp[0] = flash_info.jedec_id[0];
    resp[1] = flash_info.jedec_id[1];
    resp[2] = flash_info.jedec_id[2];
    resp[3] = 0;
    write_le32(&resp[4], flash_info.size);
    write_le32(&resp[8], RAM_BASE);
    write_le32(&resp[12], 0x10000);    /* 64KB sector */
    proto_send(RSP_INFO, resp, 16);
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
     * RAM and flash can use byte access. */
    int io_region = (addr >= 0x10000000 && addr < 0x13000000);

    uint16_t seq = 0;
    uint32_t offset = 0;
    uint8_t pkt[MAX_PAYLOAD];

    while (offset < size) {
        uint32_t chunk = size - offset;
        if (chunk > MAX_PAYLOAD - 2) chunk = MAX_PAYLOAD - 2;

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

    const uint8_t *ptr = (const uint8_t *)addr;
    uint32_t c = crc32(0, ptr, size);
    uint8_t resp[4];
    write_le32(resp, c);
    proto_send(RSP_CRC32, resp, 4);
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
            /* Backpressure: COBS-framed ACK after each DATA packet.
             * Host waits for this before sending next packet. */
            proto_send_ack(ACK_OK);
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
        flash_erase_sector(addr + i * sector_sz);

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
 * ARM32 position-independent trampoline (machine code).
 * Copies r2 bytes from r1 to r0, then branches to r0-r2 (original dst).
 *
 *   mov r3, r0          @ save dst
 *   cmp r2, #0
 *   beq done
 * loop:
 *   ldrb r4, [r1], #1
 *   strb r4, [r0], #1
 *   subs r2, r2, #1
 *   bne loop
 * done:
 *   bx r3               @ jump to saved dst
 */
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
            /* Backpressure ACK — host must wait before sending next */
            proto_send_ack(ACK_OK);
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

#define SCAN_BATCH_MAX    8  /* sectors per RSP_SCAN packet — small for live progress */

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

    /* Process sectors in batches */
    uint8_t result_buf[SCAN_BATCH_MAX * 5];
    uint32_t buf_idx = 0;

    for (uint32_t s = 0; s < num_sectors; s++) {
        const uint8_t *ptr = (const uint8_t *)(FLASH_MEM + s * sector_sz);
        uint8_t status = SCAN_GOOD;

        proto_drain_fifo();

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
        } else {
            /* Pass 2: stability check — re-read CRC */
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

    /* Wait for host to confirm with any valid command within 3 seconds.
     * If nothing arrives, revert to 115200 — the host may have failed
     * to switch or the new baud rate doesn't work on this link. */
    uint8_t pkt[MAX_PAYLOAD + 16];
    uint32_t pkt_len = 0;
    uint8_t cmd = proto_recv(pkt, &pkt_len, 3000);
    if (cmd == 0) {
        /* No valid command — revert */
        uart_set_baud(115200);
        while (uart_readable()) uart_getc();
        at_default_baud = 1;
    } else {
        /* Got a valid command at new baud — confirmed working */
        at_default_baud = (baud == 115200);
        switch (cmd) {
            case CMD_INFO:  handle_info(); break;
            case CMD_READ:  handle_read(pkt, pkt_len); break;
            case CMD_WRITE: handle_write(pkt, pkt_len); break;
            case CMD_CRC32: handle_crc32_cmd(pkt, pkt_len); break;
            case CMD_SCAN:  handle_scan(pkt, pkt_len); break;
            default:        proto_send_ack(ACK_OK); break;
        }
    }
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
                if (baud_idle >= 20) {
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
            case CMD_SET_BAUD:
                handle_set_baud(cmd_buf, data_len);
                break;
            case CMD_REBOOT:
                /* Rejected — watchdog reset re-enters bootrom on serial
                 * boot pin, killing the agent with no recovery. Use
                 * SELFUPDATE to reload, or physical power cycle. */
                proto_send_ack(ACK_FLASH_ERROR);
                break;
            default:
                proto_send_ack(ACK_CRC_ERROR);
                break;
        }
    }
}
