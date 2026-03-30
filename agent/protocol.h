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

/* Responses (device → host) */
#define RSP_INFO    0x81
#define RSP_DATA    0x82
#define RSP_ACK     0x83
#define RSP_CRC32   0x84
#define RSP_READY   0x85

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

#endif /* PROTOCOL_H */
