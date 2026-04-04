#include <stdio.h>
#include <string.h>
#include <stdint.h>
#include <stdlib.h>
#include "cobs.h"

extern uint32_t crc32(uint32_t, const uint8_t*, uint32_t);


int main(int argc, char *argv[]) {
    FILE *f = fopen(argv[1], "rb");
    fseek(f, 0, SEEK_END); long sz = ftell(f); fseek(f, 0, SEEK_SET);
    uint8_t *fw = malloc(sz); fread(fw, 1, sz, f); fclose(f);

    /* Block 3 (offset 49152), packet 29 (offset 49152 + 29*512 = 63,616) */
    uint32_t off = 49152 + 29 * 512;
    uint8_t chunk[512];
    memcpy(chunk, &fw[off], 512);

    /* Build DATA packet: cmd(1) + seq(2) + data(512) + crc(4) = 519 bytes */
    uint8_t raw[520];
    raw[0] = 0x82; /* RSP_DATA */
    raw[1] = 29; raw[2] = 0; /* seq=29 LE */
    memcpy(&raw[3], chunk, 512);
    uint32_t raw_len = 515;
    uint32_t c = crc32(0, raw, raw_len);
    raw[raw_len+0] = c & 0xFF;
    raw[raw_len+1] = (c >> 8) & 0xFF;
    raw[raw_len+2] = (c >> 16) & 0xFF;
    raw[raw_len+3] = (c >> 24) & 0xFF;
    raw_len += 4; /* 519 */

    printf("Raw payload: %u bytes, CRC=0x%08x\n", raw_len, c);
    printf("  raw[515..518] (CRC bytes): %02x %02x %02x %02x\n",
           raw[515], raw[516], raw[517], raw[518]);

    /* COBS encode */
    uint8_t encoded[600];
    uint32_t enc_len = cobs_encode(raw, raw_len, encoded);
    printf("COBS encoded: %u bytes\n", enc_len);

    /* COBS decode */
    uint8_t decoded[600];
    uint32_t dec_len = cobs_decode(encoded, enc_len, decoded);
    printf("COBS decoded: %u bytes\n", dec_len);

    /* Compare */
    if (dec_len != raw_len) {
        printf("LENGTH MISMATCH: decoded=%u raw=%u\n", dec_len, raw_len);
    }
    
    int match = (dec_len == raw_len) && (memcmp(decoded, raw, raw_len) == 0);
    printf("Roundtrip match: %s\n", match ? "YES" : "NO");

    if (!match) {
        for (uint32_t i = 0; i < raw_len && i < dec_len; i++) {
            if (decoded[i] != raw[i]) {
                printf("  First diff at byte %u: decoded=0x%02x raw=0x%02x\n",
                       i, decoded[i], raw[i]);
                break;
            }
        }
    }

    /* Extract CRC from decoded */
    if (dec_len >= 5) {
        uint32_t plen = dec_len - 4;
        uint32_t exp = (uint32_t)decoded[plen] | ((uint32_t)decoded[plen+1]<<8) |
                       ((uint32_t)decoded[plen+2]<<16) | ((uint32_t)decoded[plen+3]<<24);
        uint32_t act = crc32(0, decoded, plen);
        printf("CRC check: actual=0x%08x expected=0x%08x %s\n",
               act, exp, act == exp ? "OK" : "MISMATCH");
    }

    free(fw);
    return match ? 0 : 1;
}
