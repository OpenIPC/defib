/*
 * Test C COBS decode + CRC32 against real firmware data.
 * Reads firmware from stdin, builds COBS packets for each 512B block,
 * decodes them, verifies CRC. Compiled with ASAN to catch memory bugs.
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <stdint.h>
#include "cobs.h"
#include "protocol.h"

/* Replicate build_packet: cmd + data + crc32, COBS encode, add delimiter */
static uint32_t build_packet(uint8_t cmd, const uint8_t *data, uint32_t len,
                             uint8_t *out) {
    uint8_t raw[1100];
    raw[0] = cmd;
    memcpy(&raw[1], data, len);
    uint32_t raw_len = 1 + len;

    uint32_t c = crc32(0, raw, raw_len);
    raw[raw_len+0] = (c >> 0) & 0xFF;
    raw[raw_len+1] = (c >> 8) & 0xFF;
    raw[raw_len+2] = (c >> 16) & 0xFF;
    raw[raw_len+3] = (c >> 24) & 0xFF;
    raw_len += 4;

    uint32_t cobs_len = cobs_encode(raw, raw_len, out);
    out[cobs_len] = 0x00;
    return cobs_len + 1;
}

/* Replicate proto_recv parsing */
static int parse_packet(const uint8_t *cobs_data, uint32_t cobs_len,
                        uint8_t *cmd_out, uint8_t *data_out, uint32_t *data_len) {
    uint8_t decoded[1100];
    uint32_t dec_len = cobs_decode(cobs_data, cobs_len, decoded);
    if (dec_len < 5) return -1;

    uint32_t payload_len = dec_len - 4;
    uint32_t expected = (uint32_t)decoded[payload_len] |
                       ((uint32_t)decoded[payload_len+1] << 8) |
                       ((uint32_t)decoded[payload_len+2] << 16) |
                       ((uint32_t)decoded[payload_len+3] << 24);
    uint32_t actual = crc32(0, decoded, payload_len);
    if (actual != expected) {
        fprintf(stderr, "CRC mismatch at payload_len=%u: actual=%08x expected=%08x\n",
                payload_len, actual, expected);
        return -2;
    }

    *cmd_out = decoded[0];
    *data_len = payload_len - 1;
    memcpy(data_out, &decoded[1], *data_len);
    return 0;
}

/* Replicate handle_write's receive + CRC verify */
static int simulate_write_block(const uint8_t *block, uint32_t block_size,
                                uint32_t expected_crc) {
    /* For each 512B chunk, build DATA packet, parse it, copy to dest */
    uint8_t dest[65536];
    uint32_t received = 0;

    for (uint32_t pkt = 0; pkt * 512 < block_size; pkt++) {
        uint32_t offset = pkt * 512;
        uint32_t chunk = block_size - offset;
        if (chunk > 512) chunk = 512;

        /* Build DATA packet: seq(2) + data */
        uint8_t payload[520];
        payload[0] = (pkt >> 0) & 0xFF;
        payload[1] = (pkt >> 8) & 0xFF;
        memcpy(&payload[2], &block[offset], chunk);

        uint8_t pkt_buf[1100];
        uint32_t pkt_len = build_packet(0x82 /* RSP_DATA */, payload, 2 + chunk, pkt_buf);

        /* Parse packet (agent side) */
        uint8_t cmd;
        uint8_t data[1100];
        uint32_t data_len;
        int rc = parse_packet(pkt_buf, pkt_len - 1, &cmd, data, &data_len);
        if (rc != 0) {
            fprintf(stderr, "Block parse failed at pkt %u, rc=%d\n", pkt, rc);
            return -1;
        }

        /* Copy to dest (like handle_write) */
        uint32_t copy_len = data_len - 2; /* Skip seq */
        for (uint32_t i = 0; i < copy_len && received < block_size; i++) {
            dest[received++] = data[2 + i];
        }
    }

    /* Verify CRC (like handle_write) */
    uint32_t actual_crc = crc32(0, dest, block_size);
    if (actual_crc != expected_crc) {
        fprintf(stderr, "Final CRC mismatch: actual=%08x expected=%08x received=%u/%u\n",
                actual_crc, expected_crc, received, block_size);
        /* Find first diff */
        for (uint32_t i = 0; i < block_size; i++) {
            if (dest[i] != block[i]) {
                fprintf(stderr, "First diff at byte %u: dest=%02x src=%02x\n",
                        i, dest[i], block[i]);
                break;
            }
        }
        return -2;
    }

    return 0;
}

int main(int argc, char *argv[]) {
    if (argc < 2) {
        fprintf(stderr, "Usage: %s firmware.bin\n", argv[0]);
        return 1;
    }

    FILE *f = fopen(argv[1], "rb");
    if (!f) { perror("open"); return 1; }
    fseek(f, 0, SEEK_END);
    long size = ftell(f);
    fseek(f, 0, SEEK_SET);

    uint8_t *firmware = malloc(size);
    fread(firmware, 1, size, f);
    fclose(f);

    printf("Firmware: %ld bytes\n", size);

    /* Test every 512B block as a standalone write */
    int failures = 0;
    for (long offset = 0; offset < size && offset < 256 * 1024; offset += 512) {
        uint32_t block_size = 512;
        if (offset + block_size > size) block_size = size - offset;

        uint32_t expected_crc = crc32(0, &firmware[offset], block_size);
        int rc = simulate_write_block(&firmware[offset], block_size, expected_crc);
        if (rc != 0) {
            printf("FAIL at offset %ld (block %ld)\n", offset, offset / 512);
            failures++;
        }
    }

    /* Also test 16KB blocks (like write_memory with 16KB WRITE_MAX_TRANSFER) */
    printf("\nTesting 16KB blocks:\n");
    for (long offset = 0; offset < size && offset < 256 * 1024; offset += 16384) {
        uint32_t block_size = 16384;
        if (offset + block_size > size) block_size = size - offset;

        uint32_t expected_crc = crc32(0, &firmware[offset], block_size);
        int rc = simulate_write_block(&firmware[offset], block_size, expected_crc);
        if (rc != 0) {
            printf("FAIL at offset %ld\n", offset);
            failures++;
        } else {
            printf("  %ldKB: OK\n", offset / 1024);
        }
    }

    if (failures == 0) {
        printf("\nAll blocks pass!\n");
    } else {
        printf("\n%d failures\n", failures);
    }

    free(firmware);
    return failures ? 1 : 0;
}
