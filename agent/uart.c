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

/* Bootrom-provided divisors that produce a working UART_BAUD. Captured
 * once in uart_init and used by uart_set_baud to scale to other rates,
 * which avoids depending on a (possibly wrong) compile-time UART_CLOCK
 * constant. A combined "BRD64" value (IBRD * 64 + FBRD) is what PL011
 * uses internally; baud_rate = uart_clk / BRD64. Rescaling preserves
 * the ratio so a 115200→921600 step turns a working IBRD=13/FBRD=1 into
 * IBRD=1/FBRD=40 etc, regardless of what uart_clk actually is. */
static uint32_t bootrom_brd64 = 0;

void uart_init(void) {
    /* Wait for any in-flight TX to drain before we touch CR. */
    while (!(uart_reg(UART_FR) & UART_FR_TXFE)) {}
    while (uart_reg(UART_FR) & UART_FR_BUSY) {}

    uart_reg(UART_CR) = 0;

#ifdef UART_CKSEL_REG
    /* V1-era HiSilicon (hi3520dv200, ...): bootrom hands us a UART
     * running off a slow ~2 MHz reference. Vendor Linux mach-godarm
     * clears bit UART_CKSEL_BIT of CRG+0xE4 to switch UART onto the
     * APB clock (~99 MHz at busclk=198 MHz), which is the only way to
     * reach baud rates above ~125000. After the switch, the existing
     * IBRD/FBRD would produce a wildly wrong baud rate, so we
     * unconditionally reprogram from compile-time UART_CLOCK. */
    {
        volatile uint32_t *cksel = (volatile uint32_t *)UART_CKSEL_REG;
        *cksel &= ~(1u << UART_CKSEL_BIT);
    }
    {
        uint32_t divisor = UART_CLOCK / (16 * UART_BAUD);
        uint32_t frac = ((UART_CLOCK % (16 * UART_BAUD)) * 64 +
                         (16 * UART_BAUD) / 2) / (16 * UART_BAUD);
        uart_reg(UART_IBRD) = divisor;
        uart_reg(UART_FBRD) = frac & 0x3F;
        bootrom_brd64 = divisor * 64 + (frac & 0x3F);
    }
#else
    /* V3+/V4+/V5/V6: preserve the IBRD/FBRD the bootrom/SPL left behind.
     * They already produce a working UART_BAUD on whatever clock the
     * SoC's UART is actually wired to — recomputing from a possibly-
     * wrong compile-time UART_CLOCK constant breaks the link on chips
     * where vendor SPL doesn't program UART. */
    uint32_t ibrd = uart_reg(UART_IBRD);
    uint32_t fbrd = uart_reg(UART_FBRD);
    if (ibrd == 0 && fbrd == 0) {
        uint32_t divisor = UART_CLOCK / (16 * UART_BAUD);
        uint32_t frac = ((UART_CLOCK % (16 * UART_BAUD)) * 64 +
                         (16 * UART_BAUD) / 2) / (16 * UART_BAUD);
        uart_reg(UART_IBRD) = divisor;
        uart_reg(UART_FBRD) = frac & 0x3F;
        bootrom_brd64 = divisor * 64 + (frac & 0x3F);
    } else {
        uart_reg(UART_IBRD) = ibrd;
        uart_reg(UART_FBRD) = fbrd;
        bootrom_brd64 = ibrd * 64 + (fbrd & 0x3F);
    }
#endif

    /* 8N1, FIFO enabled — overwrite LCR_H so any stale break/parity
     * bits the bootrom left set don't corrupt the framing. */
    uart_reg(UART_LCR_H) = UART_LCR_WLEN8 | UART_LCR_FEN;

    /* Enable UART, TX, RX — explicitly NO loopback (bit 7 = 0) */
    uart_reg(UART_CR) = UART_CR_UARTEN | UART_CR_TXE | UART_CR_RXE;

    /* Clear any pending interrupts */
    uart_reg(UART_ICR) = 0x7FF;
    /* Mask all interrupts (we poll) */
    uart_reg(UART_IMSC) = 0;
}

void uart_set_baud(uint32_t baud) {
    /* Wait for TX FIFO to drain — but with a bounded timeout. If the
     * previous baud was wrong, the FIFO may never clock out (FR.TXFE
     * never asserts) and an unbounded wait would hang the agent in
     * a way that requires a power-cycle to recover. After timeout we
     * just blow away any pending bytes via UART disable below. */
    {
        volatile uint32_t t = 200000;
        while (t-- && !(uart_reg(UART_FR) & UART_FR_TXFE)) {}
        t = 200000;
        while (t-- && (uart_reg(UART_FR) & UART_FR_BUSY)) {}
    }

    /* Disable UART before changing baud */
    uart_reg(UART_CR) = 0;

    uint32_t divisor, frac;

    /* Prefer scaling from the bootrom-captured divisor (works even when
     * UART_CLOCK is wrong) over the compile-time UART_CLOCK constant. */
    if (bootrom_brd64 != 0 && baud != 0) {
        /* new_brd64 = bootrom_brd64 * UART_BAUD / new_baud, with rounding */
        uint64_t new_brd64 =
            ((uint64_t)bootrom_brd64 * UART_BAUD + baud / 2) / baud;
        divisor = (uint32_t)(new_brd64 / 64);
        frac    = (uint32_t)(new_brd64 % 64);
    } else {
        divisor = UART_CLOCK / (16 * baud);
        uint32_t remainder = UART_CLOCK % (16 * baud);
        frac = (remainder * 64 + (16 * baud) / 2) / (16 * baud);
    }

    /* PL011: IBRD must be 1..65535. IBRD=0 disables the baud generator,
     * which kills the link with no recovery short of a power cycle. If
     * the requested baud is too high for the current UART clock, give
     * up and re-enable at the previous divisors so the link survives. */
    if (divisor < 1 || divisor > 0xFFFF) {
        uart_reg(UART_LCR_H) = UART_LCR_WLEN8 | UART_LCR_FEN;
        uart_reg(UART_CR) = UART_CR_UARTEN | UART_CR_TXE | UART_CR_RXE;
        uart_reg(UART_ICR) = 0x7FF;
        return;
    }

    uart_reg(UART_IBRD) = divisor;
    uart_reg(UART_FBRD) = frac & 0x3F;

    /* Must write LCR_H after baud rate to latch the divisors */
    uart_reg(UART_LCR_H) = UART_LCR_WLEN8 | UART_LCR_FEN;

    /* Re-enable */
    uart_reg(UART_CR) = UART_CR_UARTEN | UART_CR_TXE | UART_CR_RXE;
    uart_reg(UART_ICR) = 0x7FF;
}

/* Defined in protocol.c */
void proto_drain_fifo(void);

void uart_putc(uint8_t ch) {
    /* Wait until TX FIFO has space. Drain RX into software buffer
     * while waiting — prevents FIFO overflow during sustained
     * bidirectional traffic (backpressure ACK + incoming DATA). */
    volatile uint32_t timeout = 100000;
    while ((uart_reg(UART_FR) & UART_FR_TXFF) && timeout > 0) {
        proto_drain_fifo();
        timeout--;
    }
    uart_reg(UART_DR) = ch;
}

int uart_putc_safe(uint8_t ch) {
    /* Wait until TX FIFO has space, with timeout. Returns 0 on success, -1 on timeout. */
    volatile uint32_t timeout = 100000;
    while ((uart_reg(UART_FR) & UART_FR_TXFF) && timeout > 0) { timeout--; }
    if (timeout == 0) return -1;
    uart_reg(UART_DR) = ch;
    return 0;
}

uint8_t uart_getc(void) {
    /* Wait until RX FIFO has data */
    while (uart_reg(UART_FR) & UART_FR_RXFE) {}
    uint32_t dr = uart_reg(UART_DR);
    if (dr & UART_DR_ERR) uart_reg(UART_RSR) = 0;  /* Clear errors */
    return (uint8_t)(dr & 0xFF);
}

int uart_getc_safe(void) {
    /* Non-blocking read. Returns byte (0-255) or -1 if empty, -2 if error (BREAK/framing). */
    if (uart_reg(UART_FR) & UART_FR_RXFE) return -1;
    uint32_t dr = uart_reg(UART_DR);
    if (dr & UART_DR_ERR) {
        uart_reg(UART_RSR) = 0;  /* Clear errors */
        if (dr & (UART_DR_BE | UART_DR_FE)) return -2;  /* BREAK or framing error */
    }
    return (int)(dr & 0xFF);
}

int uart_readable(void) {
    return !(uart_reg(UART_FR) & UART_FR_RXFE);
}

void uart_clear_errors(void) {
    uart_reg(UART_RSR) = 0;
}

void uart_drain_rx(void) {
    while (uart_readable()) {
        (void)uart_reg(UART_DR);
    }
    uart_reg(UART_RSR) = 0;
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
