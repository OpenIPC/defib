/*
 * PL011 UART driver — bare-metal, no OS.
 *
 * UART is already configured by the bootrom at 115200 8N1.
 * We just ensure it's enabled and use it.
 */

#include "uart.h"

/* Simple busy-wait delay (~1ms at typical ARM clock) */
static void delay_us(uint32_t us) {
    volatile uint32_t count = us * 10;
    while (count--) {}
}

void uart_init(void) {
    /* UART is already configured by bootrom. Just ensure it's enabled. */
    uint32_t cr = uart_reg(UART_CR);
    if (!(cr & UART_CR_UARTEN)) {
        /* Disable UART first */
        uart_reg(UART_CR) = 0;

        /* Set baud rate: divisor = clock / (16 * baud) */
        uint32_t divisor = UART_CLOCK / (16 * UART_BAUD);
        uint32_t frac = ((UART_CLOCK % (16 * UART_BAUD)) * 64 + (16 * UART_BAUD) / 2) / (16 * UART_BAUD);
        uart_reg(UART_IBRD) = divisor;
        uart_reg(UART_FBRD) = frac & 0x3F;

        /* 8N1, FIFO enabled */
        uart_reg(UART_LCR_H) = UART_LCR_WLEN8 | UART_LCR_FEN;

        /* Enable UART, TX, RX */
        uart_reg(UART_CR) = UART_CR_UARTEN | UART_CR_TXE | UART_CR_RXE;
    }

    /* Clear any pending interrupts */
    uart_reg(UART_ICR) = 0x7FF;
    /* Mask all interrupts (we poll) */
    uart_reg(UART_IMSC) = 0;
}

void uart_putc(uint8_t ch) {
    /* Wait until TX FIFO has space */
    while (uart_reg(UART_FR) & UART_FR_TXFF) {}
    uart_reg(UART_DR) = ch;
}

uint8_t uart_getc(void) {
    /* Wait until RX FIFO has data */
    while (uart_reg(UART_FR) & UART_FR_RXFE) {}
    return (uint8_t)(uart_reg(UART_DR) & 0xFF);
}

int uart_readable(void) {
    return !(uart_reg(UART_FR) & UART_FR_RXFE);
}

void uart_puts(const char *s) {
    while (*s) {
        uart_putc((uint8_t)*s++);
    }
}

void uart_write(const uint8_t *buf, uint32_t len) {
    for (uint32_t i = 0; i < len; i++) {
        uart_putc(buf[i]);
    }
}

uint32_t uart_read(uint8_t *buf, uint32_t max_len, uint32_t timeout_ms) {
    uint32_t count = 0;
    uint32_t deadline = timeout_ms * 100; /* Rough timer */

    while (count < max_len) {
        if (uart_readable()) {
            buf[count++] = uart_getc();
            deadline = timeout_ms * 100; /* Reset timeout on data */
        } else {
            if (deadline == 0 && count > 0) break;
            if (deadline > 0) deadline--;
            delay_us(10);
        }
    }
    return count;
}
