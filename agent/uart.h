/*
 * PL011 UART driver for HiSilicon SoCs.
 *
 * All HiSilicon SoCs use ARM PrimeCell PL011 UART.
 * Base addresses vary by generation — selected at compile time.
 */

#ifndef UART_H
#define UART_H

#include <stdint.h>

/* PL011 register offsets */
#define UART_DR         0x00    /* Data register */
#define UART_RSR        0x04    /* Receive status / error clear */
#define UART_FR         0x18    /* Flag register */
#define UART_IBRD       0x24    /* Integer baud rate divisor */
#define UART_FBRD       0x28    /* Fractional baud rate divisor */
#define UART_LCR_H      0x2C    /* Line control */
#define UART_CR         0x30    /* Control register */
#define UART_IMSC       0x38    /* Interrupt mask */
#define UART_ICR        0x44    /* Interrupt clear */

/* Data register error bits (read) */
#define UART_DR_FE      (1 << 8)    /* Framing error */
#define UART_DR_PE      (1 << 9)    /* Parity error */
#define UART_DR_BE      (1 << 10)   /* Break error */
#define UART_DR_OE      (1 << 11)   /* Overrun error */
#define UART_DR_ERR     (UART_DR_FE | UART_DR_PE | UART_DR_BE | UART_DR_OE)

/* Flag register bits */
#define UART_FR_TXFF    (1 << 5)    /* TX FIFO full */
#define UART_FR_RXFE    (1 << 4)    /* RX FIFO empty */
#define UART_FR_TXFE    (1 << 7)    /* TX FIFO empty */
#define UART_FR_BUSY    (1 << 3)    /* UART busy */

/* Control register bits */
#define UART_CR_UARTEN  (1 << 0)    /* UART enable */
#define UART_CR_TXE     (1 << 8)    /* TX enable */
#define UART_CR_RXE     (1 << 9)    /* RX enable */

/* Line control bits */
#define UART_LCR_WLEN8  (3 << 5)    /* 8-bit word length */
#define UART_LCR_FEN    (1 << 4)    /* FIFO enable */

/* UART base addresses by SoC generation (selected via -DUART_BASE=...) */
#ifndef UART_BASE
/* Default: V4 generation (hi3516ev200/ev300) */
#define UART_BASE       0x12040000
#endif

#ifndef UART_CLOCK
#define UART_CLOCK      24000000    /* 24 MHz default */
#endif

#define UART_BAUD       115200

/* Inline register access */
#define uart_reg(off)   (*(volatile uint32_t *)(UART_BASE + (off)))

void uart_init(void);
void uart_set_baud(uint32_t baud);
void uart_putc(uint8_t ch);
int uart_putc_safe(uint8_t ch);
uint8_t uart_getc(void);
int uart_getc_safe(void);
int uart_readable(void);
void uart_clear_errors(void);
void uart_drain_rx(void);
void uart_puts(const char *s);
void uart_write(const uint8_t *buf, uint32_t len);
uint32_t uart_read(uint8_t *buf, uint32_t max_len, uint32_t timeout_ms);

#endif /* UART_H */
