/*
 * Flash agent protocol — command handling.
 */

#ifndef PROTOCOL_H
#define PROTOCOL_H

#include <stdint.h>

/* Commands (host → device) */
#define CMD_INFO    0x01
#define CMD_READ    0x02
#define CMD_WRITE   0x03
#define CMD_ERASE   0x04
#define CMD_CRC32   0x05
#define CMD_REBOOT      0x06
#define CMD_SELFUPDATE  0x07
#define CMD_SET_BAUD    0x08
#define CMD_SCAN        0x09
#define CMD_FLASH_PROGRAM 0x0A
#define CMD_FLASH_STREAM  0x0B
#define CMD_MARK_BAD      0x0C  /* NAND only: write 0x00 to OOB[0] of page 0 of a block */
#define CMD_MEMBW         0x0D  /* DDR bandwidth test (ARMv7 only): see handle_membw */

/* Responses (device → host) */
#define RSP_INFO    0x81
#define RSP_DATA    0x82
#define RSP_ACK     0x83
#define RSP_CRC32   0x84
#define RSP_READY   0x85
#define RSP_SCAN    0x86
#define RSP_MEMBW   0x87

/* ACK status codes */
#define ACK_OK          0x00
#define ACK_CRC_ERROR   0x01
#define ACK_FLASH_ERROR 0x02

/* Max payload in a single packet */
#define MAX_PAYLOAD     1024

/* Send a COBS-framed packet with CRC32 */
void proto_send(uint8_t cmd, const uint8_t *data, uint32_t len);

/* Receive a COBS-framed packet. Returns command byte, fills data/len.
 * Returns 0 on timeout, cmd byte on success. */
uint8_t proto_recv(uint8_t *data, uint32_t *len, uint32_t timeout_ms);

/* Send READY announcement */
void proto_send_ready(void);

/* Send ACK with status */
void proto_send_ack(uint8_t status);

/* CRC32 (same as zlib) */
uint32_t crc32(uint32_t crc, const uint8_t *buf, uint32_t len);

/* Drain PL011 FIFO into software buffer. Call frequently during
 * long computations to prevent 16-byte hardware FIFO overflow. */
void proto_drain_fifo(void);

/* Reset all RX buffers (software + hardware) */
void proto_reset_rx(void);

#endif /* PROTOCOL_H */
