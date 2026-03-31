/*
 * COBS encode/decode — minimal implementation for embedded.
 */

#include "cobs.h"

uint32_t cobs_encode(const uint8_t *input, uint32_t len, uint8_t *output) {
    uint32_t out_idx = 0;
    uint32_t code_idx = out_idx++;
    uint8_t code = 1;

    for (uint32_t i = 0; i < len; i++) {
        if (input[i] == 0x00) {
            output[code_idx] = code;
            code_idx = out_idx++;
            code = 1;
        } else {
            output[out_idx++] = input[i];
            code++;
            if (code == 0xFF) {
                output[code_idx] = code;
                code_idx = out_idx++;
                code = 1;
            }
        }
    }
    output[code_idx] = code;

    return out_idx;
}

uint32_t cobs_decode(const uint8_t *input, uint32_t len, uint8_t *output) {
    uint32_t out_idx = 0;
    uint32_t idx = 0;

    while (idx < len) {
        uint8_t code = input[idx++];
        if (code == 0) return 0; /* Error: zero in encoded data */

        for (uint8_t i = 1; i < code; i++) {
            if (idx >= len) return 0; /* Truncated */
            output[out_idx++] = input[idx++];
        }
        if (code < 0xFF && idx < len) {
            output[out_idx++] = 0x00;
        }
    }

    /* Remove trailing zero if present */
    if (out_idx > 0 && output[out_idx - 1] == 0x00) {
        out_idx--;
    }

    return out_idx;
}
