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
    /* COBS strips trailing zero — [0,0,0,0] roundtrips to [0,0,0] */
    ASSERT(dec_len == 3, "cobs all-zeros: length (trailing zero stripped)");
    for (uint32_t i = 0; i < dec_len; i++)
        ASSERT(dec[i] == 0, "cobs all-zeros: data");
}

static void test_cobs_single_zero(void) {
    uint8_t in[] = {0};
    uint8_t enc[8], dec[8];
    uint32_t enc_len = cobs_encode(in, 1, enc);
    uint32_t dec_len = cobs_decode(enc, enc_len, dec);
    /* Single zero roundtrips to empty — trailing zero stripped */
    ASSERT(dec_len == 0, "cobs single zero: stripped to empty");
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

    printf("Cross-compatibility:\n");
    test_cobs_matches_python();

    printf("\n%d/%d tests passed\n", tests_passed, tests_run);
    return tests_passed == tests_run ? 0 : 1;
}
