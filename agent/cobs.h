/*
 * COBS (Consistent Overhead Byte Stuffing) — C implementation.
 */

#ifndef COBS_H
#define COBS_H

#include <stdint.h>

/*
 * Encode data with COBS. Output will contain no zero bytes.
 * Returns number of bytes written to output.
 * output must be at least (len + len/254 + 1) bytes.
 */
uint32_t cobs_encode(const uint8_t *input, uint32_t len, uint8_t *output);

/*
 * Decode COBS-encoded data (without trailing 0x00 delimiter).
 * Returns number of decoded bytes, or 0 on error.
 */
uint32_t cobs_decode(const uint8_t *input, uint32_t len, uint8_t *output);

#endif /* COBS_H */
