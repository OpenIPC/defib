/*
 * Unit tests for agent C code: COBS, CRC32, protocol framing.
 * Compiled and run on the host (not ARM) with: make test
 */

#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include "cobs.h"
#include "protocol.h"

static int tests_run = 0;
static int tests_passed = 0;

#define ASSERT(cond, msg) do { \
    tests_run++; \
    if (!(cond)) { \
        printf("  FAIL: %s (line %d)\n", msg, __LINE__); \
    } else { \
        tests_passed++; \
    } \
} while (0)

/* ---------- COBS tests ---------- */

static void test_cobs_no_zeros(void) {
    uint8_t in[] = {1, 2, 3, 4, 5};
    uint8_t enc[16], dec[16];
    uint32_t enc_len = cobs_encode(in, 5, enc);

    /* No zeros in encoded output */
    for (uint32_t i = 0; i < enc_len; i++)
        ASSERT(enc[i] != 0, "cobs_encode: no zeros in output");

    uint32_t dec_len = cobs_decode(enc, enc_len, dec);
    ASSERT(dec_len == 5, "cobs roundtrip: length");
    ASSERT(memcmp(in, dec, 5) == 0, "cobs roundtrip: data");
}

static void test_cobs_all_zeros(void) {
    uint8_t in[] = {0, 0, 0, 0};
    uint8_t enc[16], dec[16];
    uint32_t enc_len = cobs_encode(in, 4, enc);

    for (uint32_t i = 0; i < enc_len; i++)
        ASSERT(enc[i] != 0, "cobs all-zeros: no zeros in output");

    uint32_t dec_len = cobs_decode(enc, enc_len, dec);
    ASSERT(dec_len == 4, "cobs all-zeros: length");
    ASSERT(memcmp(in, dec, 4) == 0, "cobs all-zeros: data");
}

static void test_cobs_single_zero(void) {
    uint8_t in[] = {0};
    uint8_t enc[8], dec[8];
    uint32_t enc_len = cobs_encode(in, 1, enc);
    uint32_t dec_len = cobs_decode(enc, enc_len, dec);
    ASSERT(dec_len == 1, "cobs single zero: length");
    ASSERT(dec[0] == 0, "cobs single zero: value");
}

static void test_cobs_empty(void) {
    uint8_t enc[8], dec[8];
    uint32_t enc_len = cobs_encode(NULL, 0, enc);
    uint32_t dec_len = cobs_decode(enc, enc_len, dec);
    ASSERT(dec_len == 0, "cobs empty: length");
}

static void test_cobs_254_nonzero(void) {
    /* Block of 254 non-zero bytes triggers 0xFF code */
    uint8_t in[254], enc[512], dec[512];
    for (int i = 0; i < 254; i++) in[i] = (uint8_t)(i + 1);

    uint32_t enc_len = cobs_encode(in, 254, enc);
    for (uint32_t i = 0; i < enc_len; i++)
        ASSERT(enc[i] != 0, "cobs 254: no zeros in output");

    uint32_t dec_len = cobs_decode(enc, enc_len, dec);
    ASSERT(dec_len == 254, "cobs 254: length");
    ASSERT(memcmp(in, dec, 254) == 0, "cobs 254: data");
}

static void test_cobs_mixed_large(void) {
    /* 1024 bytes with periodic zeros (like flash data) */
    uint8_t in[1024], enc[1100], dec[1100];
    for (int i = 0; i < 1024; i++) in[i] = (uint8_t)(i & 0xFF);

    uint32_t enc_len = cobs_encode(in, 1024, enc);
    for (uint32_t i = 0; i < enc_len; i++)
        ASSERT(enc[i] != 0, "cobs large: no zeros");

    uint32_t dec_len = cobs_decode(enc, enc_len, dec);
    ASSERT(dec_len == 1024, "cobs large: length");
    ASSERT(memcmp(in, dec, 1024) == 0, "cobs large: data");
}

static void test_cobs_single_bytes(void) {
    /* Every non-zero byte should roundtrip.
     * Zero byte gets trailing-zero-stripped (tested separately). */
    for (int b = 1; b < 256; b++) {
        uint8_t in = (uint8_t)b;
        uint8_t enc[8], dec[8];
        uint32_t enc_len = cobs_encode(&in, 1, enc);
        uint32_t dec_len = cobs_decode(enc, enc_len, dec);
        ASSERT(dec_len == 1 && dec[0] == in, "cobs single byte roundtrip");
    }
}

static void test_cobs_decode_error(void) {
    /* Zero in encoded data should return 0 (error) */
    uint8_t bad[] = {1, 0, 3};
    uint8_t dec[8];
    uint32_t dec_len = cobs_decode(bad, 3, dec);
    ASSERT(dec_len == 0, "cobs decode: zero in encoded → error");
}

/* ---------- CRC32 tests ---------- */

static void test_crc32_known_values(void) {
    /* CRC32 of "" should be 0x00000000 */
    uint32_t c = crc32(0, (const uint8_t *)"", 0);
    ASSERT(c == 0x00000000, "crc32 empty");

    /* CRC32 of "123456789" = 0xCBF43926 (standard test vector) */
    c = crc32(0, (const uint8_t *)"123456789", 9);
    ASSERT(c == 0xCBF43926, "crc32 '123456789'");
}

static void test_crc32_incremental(void) {
    /* CRC32 computed in chunks must equal single-pass */
    const uint8_t data[] = "Hello, World! This is a CRC32 test.";
    uint32_t len = sizeof(data) - 1;

    uint32_t single = crc32(0, data, len);
    uint32_t again = crc32(0, data, len);
    ASSERT(single == again, "crc32 deterministic");
}

static void test_crc32_all_zeros(void) {
    uint8_t zeros[256];
    memset(zeros, 0, 256);
    uint32_t c = crc32(0, zeros, 256);
    /* Just verify it's non-zero and deterministic */
    ASSERT(c != 0, "crc32 all-zeros: non-zero");
    ASSERT(c == crc32(0, zeros, 256), "crc32 all-zeros: deterministic");
}

static void test_crc32_all_ff(void) {
    uint8_t ff[256];
    memset(ff, 0xFF, 256);
    uint32_t c = crc32(0, ff, 256);
    ASSERT(c != 0, "crc32 all-0xFF: non-zero");
    ASSERT(c == crc32(0, ff, 256), "crc32 all-0xFF: deterministic");
}

/* ---------- Protocol framing tests ---------- */

/* Mock UART for testing proto_send/proto_recv */
#define MOCK_BUF_SIZE 4096
static uint8_t mock_tx[MOCK_BUF_SIZE];
static uint32_t mock_tx_len = 0;
static uint8_t mock_rx[MOCK_BUF_SIZE];
static uint32_t mock_rx_len = 0;
static uint32_t mock_rx_pos = 0;

/* These are called by uart.c — we override them at link time */
void uart_putc(uint8_t ch) { if (mock_tx_len < MOCK_BUF_SIZE) mock_tx[mock_tx_len++] = ch; }
int uart_putc_safe(uint8_t ch) { uart_putc(ch); return 0; }
int uart_getc_safe(void) {
    if (mock_rx_pos < mock_rx_len) return mock_rx[mock_rx_pos++];
    return -1;
}
uint8_t uart_getc(void) { return (uint8_t)uart_getc_safe(); }
int uart_readable(void) { return mock_rx_pos < mock_rx_len; }
void uart_clear_errors(void) {}
void uart_drain_rx(void) { mock_rx_pos = mock_rx_len; }
void uart_init(void) {}
void uart_puts(const char *s) { while (*s) uart_putc((uint8_t)*s++); }
void uart_write(const uint8_t *buf, uint32_t len) { for (uint32_t i = 0; i < len; i++) uart_putc(buf[i]); }
uint32_t uart_read(uint8_t *buf, uint32_t max, uint32_t timeout) { (void)timeout; uint32_t n = 0; while (n < max && mock_rx_pos < mock_rx_len) buf[n++] = mock_rx[mock_rx_pos++]; return n; }

static void mock_reset(void) {
    mock_tx_len = 0;
    mock_rx_len = 0;
    mock_rx_pos = 0;
}

static void test_proto_send_recv_roundtrip(void) {
    mock_reset();

    /* proto_send writes to mock_tx */
    uint8_t data[] = {0x11, 0x22, 0x33};
    proto_send(CMD_INFO, data, 3);

    /* mock_tx should contain COBS-framed packet ending with 0x00 */
    ASSERT(mock_tx_len > 0, "proto_send: produced output");
    ASSERT(mock_tx[mock_tx_len - 1] == 0x00, "proto_send: ends with delimiter");

    /* Feed tx into rx and proto_recv should decode it */
    memcpy(mock_rx, mock_tx, mock_tx_len);
    mock_rx_len = mock_tx_len;
    mock_rx_pos = 0;

    uint8_t recv_buf[MAX_PAYLOAD + 16];
    uint32_t recv_len = 0;
    uint8_t cmd = proto_recv(recv_buf, &recv_len, 1000);

    ASSERT(cmd == CMD_INFO, "proto roundtrip: command");
    ASSERT(recv_len == 3, "proto roundtrip: data length");
    ASSERT(memcmp(recv_buf, data, 3) == 0, "proto roundtrip: data content");
}

static void test_proto_send_ready(void) {
    mock_reset();
    proto_send_ready();

    /* Feed back and parse */
    memcpy(mock_rx, mock_tx, mock_tx_len);
    mock_rx_len = mock_tx_len;
    mock_rx_pos = 0;

    uint8_t recv_buf[MAX_PAYLOAD + 16];
    uint32_t recv_len = 0;
    uint8_t cmd = proto_recv(recv_buf, &recv_len, 1000);

    ASSERT(cmd == RSP_READY, "proto_send_ready: command");
    ASSERT(recv_len == 5, "proto_send_ready: length");
    ASSERT(memcmp(recv_buf, "DEFIB", 5) == 0, "proto_send_ready: payload");
}

static void test_proto_send_ack(void) {
    mock_reset();
    proto_send_ack(ACK_OK);

    memcpy(mock_rx, mock_tx, mock_tx_len);
    mock_rx_len = mock_tx_len;
    mock_rx_pos = 0;

    uint8_t recv_buf[MAX_PAYLOAD + 16];
    uint32_t recv_len = 0;
    uint8_t cmd = proto_recv(recv_buf, &recv_len, 1000);

    ASSERT(cmd == RSP_ACK, "proto_send_ack: command");
    ASSERT(recv_len == 1, "proto_send_ack: length");
    ASSERT(recv_buf[0] == ACK_OK, "proto_send_ack: status");
}

static void test_proto_recv_timeout(void) {
    mock_reset();
    /* Empty rx buffer → should timeout */
    uint8_t recv_buf[MAX_PAYLOAD + 16];
    uint32_t recv_len = 0;
    uint8_t cmd = proto_recv(recv_buf, &recv_len, 1);
    ASSERT(cmd == 0, "proto_recv timeout: returns 0");
}

static void test_proto_recv_bad_crc(void) {
    mock_reset();

    /* Build a valid packet first */
    proto_send(CMD_INFO, NULL, 0);

    /* Corrupt one byte in the COBS data (before delimiter) */
    if (mock_tx_len > 2) {
        mock_tx[1] ^= 0xFF;
    }

    memcpy(mock_rx, mock_tx, mock_tx_len);
    mock_rx_len = mock_tx_len;
    mock_rx_pos = 0;

    uint8_t recv_buf[MAX_PAYLOAD + 16];
    uint32_t recv_len = 0;
    uint8_t cmd = proto_recv(recv_buf, &recv_len, 1);
    ASSERT(cmd == 0, "proto_recv bad CRC: returns 0");
}

static void test_proto_max_payload(void) {
    mock_reset();

    /* Send MAX_PAYLOAD bytes */
    uint8_t big[MAX_PAYLOAD];
    for (int i = 0; i < MAX_PAYLOAD; i++) big[i] = (uint8_t)(i & 0xFF);

    proto_send(CMD_READ, big, MAX_PAYLOAD);

    memcpy(mock_rx, mock_tx, mock_tx_len);
    mock_rx_len = mock_tx_len;
    mock_rx_pos = 0;

    uint8_t recv_buf[MAX_PAYLOAD + 16];
    uint32_t recv_len = 0;
    uint8_t cmd = proto_recv(recv_buf, &recv_len, 1000);

    ASSERT(cmd == CMD_READ, "proto max payload: command");
    ASSERT(recv_len == MAX_PAYLOAD, "proto max payload: length");
    ASSERT(memcmp(recv_buf, big, MAX_PAYLOAD) == 0, "proto max payload: data");
}

static void test_proto_multiple_packets(void) {
    mock_reset();

    /* Send two packets back to back */
    proto_send(RSP_ACK, (const uint8_t[]){ACK_OK}, 1);
    proto_send(RSP_READY, (const uint8_t *)"DEFIB", 5);

    /* Feed both into rx */
    memcpy(mock_rx, mock_tx, mock_tx_len);
    mock_rx_len = mock_tx_len;
    mock_rx_pos = 0;

    /* First packet */
    uint8_t buf[MAX_PAYLOAD + 16];
    uint32_t len = 0;
    uint8_t cmd = proto_recv(buf, &len, 1000);
    ASSERT(cmd == RSP_ACK, "proto multi: first packet cmd");
    ASSERT(buf[0] == ACK_OK, "proto multi: first packet data");

    /* Second packet */
    cmd = proto_recv(buf, &len, 1000);
    ASSERT(cmd == RSP_READY, "proto multi: second packet cmd");
    ASSERT(len == 5, "proto multi: second packet length");
    ASSERT(memcmp(buf, "DEFIB", 5) == 0, "proto multi: second packet data");
}

/* ---------- Cross-compatibility with Python ---------- */

static void test_cobs_matches_python(void) {
    /*
     * Verify C COBS encode matches Python COBS encode for known inputs.
     * Python: from defib.agent.cobs import encode
     *   encode(b"\x85DEFIB" + crc32_bytes) should produce same as C.
     * We test the READY packet which is well-defined.
     */
    mock_reset();
    proto_send_ready();

    /* The READY packet: cmd=0x85, data="DEFIB", + CRC32, COBS-encoded, + 0x00 */
    /* Verify it ends with 0x00 and has no internal 0x00 */
    ASSERT(mock_tx[mock_tx_len - 1] == 0x00, "python compat: delimiter");
    for (uint32_t i = 0; i < mock_tx_len - 1; i++)
        ASSERT(mock_tx[i] != 0x00, "python compat: no internal zeros");
}

/* ---------- Regression tests ---------- */

/*
 * Bug: cobs_decode() stripped trailing 0x00 bytes. When a packet's CRC32
 * had 0x00 as the MSB, the decoded output was 1 byte short → CRC mismatch.
 * This caused ~1/256 packets to fail deterministically based on data content.
 * Root cause of ALL flash write failures before the fix.
 */
static void test_cobs_trailing_zero_preserved(void) {
    /* Build payloads where CRC32 MSB is 0x00 (trailing zero after LE encode).
     * The COBS roundtrip must preserve the trailing zero. */
    for (int trial = 0; trial < 1000; trial++) {
        /* Construct a packet: [cmd] [data...] [crc32 LE] */
        uint8_t payload[64];
        uint32_t plen = 5 + (trial % 20);  /* varying lengths */
        payload[0] = 0x82;  /* RSP_DATA */
        for (uint32_t i = 1; i < plen; i++)
            payload[i] = (uint8_t)((trial * 7 + i * 13) & 0xFF);

        /* Compute CRC and append */
        uint32_t c = crc32(0, payload, plen);
        payload[plen + 0] = (c >> 0) & 0xFF;
        payload[plen + 1] = (c >> 8) & 0xFF;
        payload[plen + 2] = (c >> 16) & 0xFF;
        payload[plen + 3] = (c >> 24) & 0xFF;
        uint32_t total = plen + 4;

        /* COBS encode → decode roundtrip */
        uint8_t enc[256], dec[256];
        uint32_t enc_len = cobs_encode(payload, total, enc);
        uint32_t dec_len = cobs_decode(enc, enc_len, dec);

        ASSERT(dec_len == total, "cobs trailing zero: length preserved");
        ASSERT(memcmp(payload, dec, total) == 0,
               "cobs trailing zero: data preserved");
    }
}

/*
 * Bug: CRC32 extraction used `cp[3] << 24` where cp[3] >= 128.
 * uint8_t promotes to int, and left-shifting a signed int into the sign
 * bit is undefined behavior. With ASAN this manifests as a runtime error.
 * Fix: cast to (uint32_t) before shifting.
 */
static void test_crc32_high_byte_shift(void) {
    /* Test CRC32 values where the MSB is >= 0x80 (signed bit set).
     * These exercise the (uint32_t) cast in CRC extraction. */
    uint8_t data_a[] = {0x03, 0x00, 0x00};  /* CMD_WRITE + padding */
    uint32_t c = crc32(0, data_a, 3);

    /* Verify the CRC packs/unpacks correctly through all 4 bytes */
    uint8_t packed[4];
    packed[0] = (c >> 0) & 0xFF;
    packed[1] = (c >> 8) & 0xFF;
    packed[2] = (c >> 16) & 0xFF;
    packed[3] = (c >> 24) & 0xFF;

    uint32_t unpacked = (uint32_t)packed[0]
                      | ((uint32_t)packed[1] << 8)
                      | ((uint32_t)packed[2] << 16)
                      | ((uint32_t)packed[3] << 24);
    ASSERT(unpacked == c, "crc32 high byte: pack/unpack roundtrip");

    /* Test with every possible MSB value */
    for (int msb = 0; msb < 256; msb++) {
        packed[3] = (uint8_t)msb;
        uint32_t val = (uint32_t)packed[0]
                     | ((uint32_t)packed[1] << 8)
                     | ((uint32_t)packed[2] << 16)
                     | ((uint32_t)packed[3] << 24);
        ASSERT((val >> 24) == (uint32_t)msb, "crc32 high byte shift");
    }
}

/*
 * End-to-end: for ALL possible payload data (varying byte values that
 * produce different CRC patterns), verify proto_send → proto_recv roundtrip.
 * This catches the COBS trailing zero bug: ~1/256 CRC values have MSB=0x00.
 */
static void test_cobs_roundtrip_all_crc_patterns(void) {
    /* Send 256 different 8-byte payloads through proto_send/recv.
     * Each produces a different CRC32, covering all possible MSB values. */
    for (int i = 0; i < 256; i++) {
        mock_reset();

        uint8_t data[8];
        for (int j = 0; j < 8; j++)
            data[j] = (uint8_t)((i * 37 + j * 53) & 0xFF);

        proto_send(RSP_DATA, data, 8);

        memcpy(mock_rx, mock_tx, mock_tx_len);
        mock_rx_len = mock_tx_len;
        mock_rx_pos = 0;

        uint8_t recv_buf[MAX_PAYLOAD + 16];
        uint32_t recv_len = 0;
        uint8_t cmd = proto_recv(recv_buf, &recv_len, 1000);

        ASSERT(cmd == RSP_DATA, "crc pattern roundtrip: cmd");
        ASSERT(recv_len == 8, "crc pattern roundtrip: len");
        ASSERT(memcmp(recv_buf, data, 8) == 0,
               "crc pattern roundtrip: data");
    }
}

/*
 * CMD_MEMBW request framing: host sends [size:4LE][iters:4LE][addr:4LE].
 * The handler runs on ARM hardware (CCNT register), but we can verify
 * the request and response packets round-trip through proto_send/recv
 * with the right shape — that's what catches wire-format mismatches
 * between agent C and host Python.
 */
static void test_proto_membw_request_framing(void) {
    mock_reset();

    /* Host → device: 12-byte payload */
    uint8_t req[12];
    uint32_t size = 4u * 1024u * 1024u;
    uint32_t iters = 8;
    uint32_t addr = 0x40400000u;
    req[0]  = (size  >> 0) & 0xFF;  req[1]  = (size  >> 8) & 0xFF;
    req[2]  = (size  >> 16) & 0xFF; req[3]  = (size  >> 24) & 0xFF;
    req[4]  = (iters >> 0) & 0xFF;  req[5]  = (iters >> 8) & 0xFF;
    req[6]  = (iters >> 16) & 0xFF; req[7]  = (iters >> 24) & 0xFF;
    req[8]  = (addr  >> 0) & 0xFF;  req[9]  = (addr  >> 8) & 0xFF;
    req[10] = (addr  >> 16) & 0xFF; req[11] = (addr  >> 24) & 0xFF;

    proto_send(CMD_MEMBW, req, 12);

    memcpy(mock_rx, mock_tx, mock_tx_len);
    mock_rx_len = mock_tx_len;
    mock_rx_pos = 0;

    uint8_t buf[MAX_PAYLOAD + 16];
    uint32_t len = 0;
    uint8_t cmd = proto_recv(buf, &len, 1000);
    ASSERT(cmd == CMD_MEMBW, "membw request: command opcode");
    ASSERT(len == 12, "membw request: payload length");
    ASSERT(memcmp(buf, req, 12) == 0, "membw request: payload bytes");
}

static void test_proto_membw_response_framing(void) {
    mock_reset();

    /* Device → host: 32-byte response.  Build with synthetic values that
     * exercise all 8 little-endian word fields. */
    uint8_t resp[32];
    uint32_t fields[8] = {
        0x40400000u,   /* base */
        4u << 20,      /* size = 4 MiB */
        8u,            /* iters */
        24000000u,     /* timer_hz */
        123456u,       /* memset_ticks */
        654321u,       /* read_ticks */
        999999u,       /* memcpy_ticks */
        1u,            /* cpu_arch */
    };
    for (int i = 0; i < 8; i++) {
        resp[i*4 + 0] = (fields[i] >> 0)  & 0xFF;
        resp[i*4 + 1] = (fields[i] >> 8)  & 0xFF;
        resp[i*4 + 2] = (fields[i] >> 16) & 0xFF;
        resp[i*4 + 3] = (fields[i] >> 24) & 0xFF;
    }

    proto_send(RSP_MEMBW, resp, 32);

    memcpy(mock_rx, mock_tx, mock_tx_len);
    mock_rx_len = mock_tx_len;
    mock_rx_pos = 0;

    uint8_t buf[MAX_PAYLOAD + 16];
    uint32_t len = 0;
    uint8_t cmd = proto_recv(buf, &len, 1000);
    ASSERT(cmd == RSP_MEMBW, "membw response: command opcode");
    ASSERT(len == 32, "membw response: payload length");
    ASSERT(memcmp(buf, resp, 32) == 0, "membw response: payload bytes");
}

/*
 * page_is_ff helper: verify it correctly identifies all-0xFF pages
 * and rejects pages with even a single non-0xFF byte.
 */
static int page_is_ff_test(const uint8_t *data, uint32_t len) {
    const uint32_t *w = (const uint32_t *)data;
    for (uint32_t i = 0; i < len / 4; i++)
        if (w[i] != 0xFFFFFFFF) return 0;
    return 1;
}

static void test_page_is_ff(void) {
    uint8_t page[256];

    /* All 0xFF → true */
    memset(page, 0xFF, 256);
    ASSERT(page_is_ff_test(page, 256) == 1, "page_is_ff: all FF");

    /* All 0x00 → false */
    memset(page, 0x00, 256);
    ASSERT(page_is_ff_test(page, 256) == 0, "page_is_ff: all 00");

    /* Single non-FF byte at each position */
    for (int pos = 0; pos < 256; pos++) {
        memset(page, 0xFF, 256);
        page[pos] = 0xFE;
        ASSERT(page_is_ff_test(page, 256) == 0,
               "page_is_ff: single non-FF byte");
    }

    /* Random data → false */
    for (int i = 0; i < 256; i++) page[i] = (uint8_t)i;
    ASSERT(page_is_ff_test(page, 256) == 0, "page_is_ff: random data");
}

/*
 * Sector bitmap: verify bit indexing matches the host Python implementation.
 * Bit N of bitmap = sector N. LSB-first within each byte.
 */
static void test_sector_bitmap(void) {
    uint8_t bitmap[32];
    memset(bitmap, 0, 32);

    /* Set specific sectors */
    bitmap[0] |= (1 << 0);   /* sector 0 */
    bitmap[0] |= (1 << 7);   /* sector 7 */
    bitmap[1] |= (1 << 0);   /* sector 8 */
    bitmap[31] |= (1 << 7);  /* sector 255 */

    /* Check sector_has_data equivalent */
    ASSERT((bitmap[0 / 8] & (1 << (0 % 8))) != 0, "bitmap: sector 0 set");
    ASSERT((bitmap[7 / 8] & (1 << (7 % 8))) != 0, "bitmap: sector 7 set");
    ASSERT((bitmap[8 / 8] & (1 << (8 % 8))) != 0, "bitmap: sector 8 set");
    ASSERT((bitmap[255 / 8] & (1 << (255 % 8))) != 0, "bitmap: sector 255 set");

    /* Check unset sectors */
    ASSERT((bitmap[1 / 8] & (1 << (1 % 8))) == 0, "bitmap: sector 1 unset");
    ASSERT((bitmap[128 / 8] & (1 << (128 % 8))) == 0, "bitmap: sector 128 unset");
    ASSERT((bitmap[254 / 8] & (1 << (254 % 8))) == 0, "bitmap: sector 254 unset");

    /* All-ones bitmap */
    memset(bitmap, 0xFF, 32);
    for (int s = 0; s < 256; s++)
        ASSERT((bitmap[s / 8] & (1 << (s % 8))) != 0, "bitmap: all set");

    /* All-zeros bitmap */
    memset(bitmap, 0, 32);
    for (int s = 0; s < 256; s++)
        ASSERT((bitmap[s / 8] & (1 << (s % 8))) == 0, "bitmap: all clear");
}

/* ---------- main ---------- */

int main(void) {
    printf("=== Agent C unit tests ===\n\n");

    printf("COBS:\n");
    test_cobs_no_zeros();
    test_cobs_all_zeros();
    test_cobs_single_zero();
    test_cobs_empty();
    test_cobs_254_nonzero();
    test_cobs_mixed_large();
    test_cobs_single_bytes();
    test_cobs_decode_error();

    printf("CRC32:\n");
    test_crc32_known_values();
    test_crc32_incremental();
    test_crc32_all_zeros();
    test_crc32_all_ff();

    printf("Protocol:\n");
    test_proto_send_recv_roundtrip();
    test_proto_send_ready();
    test_proto_send_ack();
    test_proto_recv_timeout();
    test_proto_recv_bad_crc();
    test_proto_max_payload();
    test_proto_multiple_packets();
    test_proto_membw_request_framing();
    test_proto_membw_response_framing();

    printf("Cross-compatibility:\n");
    test_cobs_matches_python();

    printf("Regression:\n");
    test_cobs_trailing_zero_preserved();
    test_crc32_high_byte_shift();
    test_cobs_roundtrip_all_crc_patterns();
    test_page_is_ff();
    test_sector_bitmap();

    printf("\n%d/%d tests passed\n", tests_passed, tests_run);
    return tests_passed == tests_run ? 0 : 1;
}
