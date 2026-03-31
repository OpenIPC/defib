/*
 * defib flash agent — bare-metal main loop.
 *
 * Phase 1: UART protocol testing. Flash reads via memory-mapped window.
 * Receives commands from host via COBS-framed UART protocol.
 */

#include <stdint.h>
#include "uart.h"
#include "protocol.h"

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
static int addr_readable(uint32_t addr, uint32_t size) {
    if (size == 0 || (addr + size) <= addr) return 0;  /* Overflow */
    /* RAM region: RAM_BASE to RAM_BASE + 128MB */
    if (addr >= RAM_BASE && (addr + size) <= (RAM_BASE + 128 * 1024 * 1024))
        return 1;
    /* Flash memory-mapped: only if explicitly tested and known safe.
     * Disabled by default — flash window may not be active after SPL. */
    return 0;
}

static void handle_info(void) {
    uint8_t resp[16];
    write_le32(&resp[0], 0);
    write_le32(&resp[4], 0x1000000);   /* 16MB default flash */
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

    const uint8_t *ptr = (const uint8_t *)addr;
    uint16_t seq = 0;
    uint32_t offset = 0;
    uint8_t pkt[MAX_PAYLOAD];

    while (offset < size) {
        uint32_t chunk = size - offset;
        if (chunk > MAX_PAYLOAD - 2) chunk = MAX_PAYLOAD - 2;

        pkt[0] = (seq >> 0) & 0xFF;
        pkt[1] = (seq >> 8) & 0xFF;
        for (uint32_t i = 0; i < chunk; i++)
            pkt[2 + i] = ptr[offset + i];

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

/*
 * CMD_WRITE: receive data from host and write to RAM (or flash later).
 *
 * Protocol:
 *   Host sends: CMD_WRITE [addr:4LE] [size:4LE] [expected_crc:4LE]
 *   Agent ACKs ready, then receives RSP_DATA packets.
 *   After all data, verifies CRC32. ACKs OK or CRC_ERROR.
 */
static void handle_write(const uint8_t *data, uint32_t len) {
    if (len < 12) { proto_send_ack(ACK_CRC_ERROR); return; }

    uint32_t addr = read_le32(&data[0]);
    uint32_t size = read_le32(&data[4]);
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
            /* No per-packet ACK — continuous receive for throughput */
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
        } else if (cmd == 0) {
            proto_send_ack(ACK_FLASH_ERROR);
            return;
        }
    }

    uint32_t actual_crc = crc32(0, dest, size);
    if (actual_crc != expected_crc) {
        proto_send_ack(ACK_CRC_ERROR);
        return;
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

int main(void) {
    watchdog_disable();
    uart_init();

    /* Drain any stale bytes left in UART RX FIFO from boot protocol */
    while (uart_readable()) uart_getc();

    proto_send_ready();

    uint32_t idle_count = 0;
    while (1) {
        uint32_t data_len = 0;
        uint8_t cmd = proto_recv(cmd_buf, &data_len, 500);

        if (cmd == 0) {
            idle_count++;
            /* Send READY every ~2s (4 x 500ms) so host can detect us
             * after reconnect. Suppress briefly after a command to
             * avoid interfering with multi-packet responses. */
            if (idle_count >= 4) {
                proto_send_ready();
                idle_count = 0;
            }
            continue;
        }
        idle_count = 0;

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
            case CMD_CRC32:
                handle_crc32_cmd(cmd_buf, data_len);
                break;
            case CMD_SELFUPDATE:
                handle_selfupdate(cmd_buf, data_len);
                break;
            case CMD_REBOOT:
                /* Trigger reset via watchdog */
                WDT_LOCK = WDT_UNLOCK_KEY;
                WDT_CONTROL = 3;  /* Enable interrupt + reset */
                WDT_LOCK = 0;
                while (1) {}
                break;
            default:
                proto_send_ack(ACK_CRC_ERROR);
                break;
        }
    }
}
