/*
 * PL011 UART driver — bare-metal, IRQ-driven RX.
 *
 * RX bytes are moved from the PL011 hardware FIFO into a software ring
 * buffer by the UART RX interrupt handler. This prevents FIFO overflow
 * during COBS+CRC processing, even at high baud rates.
 *
 * TX remains polled (blocking putc).
 */

#include "uart.h"

/* ---- GIC (Generic Interrupt Controller) ---- */

/* GIC base addresses for hi3516ev200/ev300 */
#ifndef GIC_DIST_BASE
#define GIC_DIST_BASE   0x10301000
#endif
#ifndef GIC_CPU_BASE
#define GIC_CPU_BASE    0x10302000
#endif

/* UART0 SPI IRQ number (from qemu-hisilicon) */
#ifndef UART_IRQ
#define UART_IRQ        7   /* SPI 7 for ev200/ev300 */
#endif

#define GICD_REG(off)   (*(volatile uint32_t *)(GIC_DIST_BASE + (off)))
#define GICC_REG(off)   (*(volatile uint32_t *)(GIC_CPU_BASE + (off)))

/* GIC Distributor registers */
#define GICD_CTLR       0x000
#define GICD_ISENABLER  0x100   /* Set-enable (32 IRQs per register) */
#define GICD_IPRIORITYR 0x400   /* Priority (4 IRQs per register) */
#define GICD_ITARGETSR  0x800   /* Target (4 IRQs per register) */

/* GIC CPU Interface registers */
#define GICC_CTLR       0x000
#define GICC_PMR        0x004   /* Priority mask */
#define GICC_IAR        0x00C   /* Interrupt acknowledge */
#define GICC_EOIR       0x010   /* End of interrupt */

/* ---- RX ring buffer (filled by IRQ handler) ---- */

#define RX_BUF_SIZE     4096    /* Must be power of 2 */
#define RX_BUF_MASK     (RX_BUF_SIZE - 1)

static volatile uint8_t rx_buf[RX_BUF_SIZE];
static volatile uint32_t rx_head;  /* Written by IRQ handler */
static volatile uint32_t rx_tail;  /* Read by main code */

/* ---- IRQ handler (called from startup.S) ---- */

void uart_irq_handler(void) {
    /* Drain PL011 RX FIFO into ring buffer */
    while (!(uart_reg(UART_FR) & UART_FR_RXFE)) {
        uint32_t dr = uart_reg(UART_DR);
        if (dr & UART_DR_ERR) {
            uart_reg(UART_RSR) = 0;
            /* Skip BREAK/framing errors */
            if (dr & (UART_DR_BE | UART_DR_FE)) continue;
        }
        uint32_t next = (rx_head + 1) & RX_BUF_MASK;
        if (next != rx_tail) {  /* Don't overwrite unread data */
            rx_buf[rx_head] = (uint8_t)(dr & 0xFF);
            rx_head = next;
        }
    }

    /* Clear RX + RT interrupts */
    uart_reg(UART_ICR) = UART_INT_RX | UART_INT_RT;
}

/* ---- GIC + UART interrupt setup ---- */

static void gic_init_uart(void) {
    /* GIC SPI numbers start at 32 in the hardware.
     * UART_IRQ is the SPI number (e.g., 7).
     * Hardware IRQ ID = 32 + UART_IRQ. */
    uint32_t irq_id = 32 + UART_IRQ;

    /* Enable GIC distributor */
    GICD_REG(GICD_CTLR) = 1;

    /* Set priority for UART IRQ (lower = higher priority, 0 = max) */
    uint32_t pri_reg = GICD_IPRIORITYR + (irq_id / 4) * 4;
    uint32_t pri_shift = (irq_id % 4) * 8;
    uint32_t pri_val = GICD_REG(pri_reg);
    pri_val &= ~(0xFF << pri_shift);
    pri_val |= (0x80 << pri_shift);  /* Priority 128 (mid) */
    GICD_REG(pri_reg) = pri_val;

    /* Target CPU 0 */
    uint32_t tgt_reg = GICD_ITARGETSR + (irq_id / 4) * 4;
    uint32_t tgt_shift = (irq_id % 4) * 8;
    uint32_t tgt_val = GICD_REG(tgt_reg);
    tgt_val &= ~(0xFF << tgt_shift);
    tgt_val |= (0x01 << tgt_shift);  /* CPU 0 */
    GICD_REG(tgt_reg) = tgt_val;

    /* Enable the UART IRQ in distributor */
    uint32_t en_reg = GICD_ISENABLER + (irq_id / 32) * 4;
    GICD_REG(en_reg) = (1 << (irq_id % 32));

    /* Enable GIC CPU interface, set priority mask to allow all */
    GICC_REG(GICC_PMR) = 0xFF;   /* Allow all priorities */
    GICC_REG(GICC_CTLR) = 1;     /* Enable CPU interface */
}

/* ---- Simple busy-wait delay ---- */

static void delay_us(uint32_t us) {
    volatile uint32_t count = us * 10;
    while (count--) {}
}

/* ---- Public API ---- */

void uart_init(void) {
    /* Reset ring buffer */
    rx_head = 0;
    rx_tail = 0;

    /* Disable UART first */
    uart_reg(UART_CR) = 0;

    /* Set baud rate */
    uint32_t divisor = UART_CLOCK / (16 * UART_BAUD);
    uint32_t frac = ((UART_CLOCK % (16 * UART_BAUD)) * 64 + (16 * UART_BAUD) / 2) / (16 * UART_BAUD);
    uart_reg(UART_IBRD) = divisor;
    uart_reg(UART_FBRD) = frac & 0x3F;

    /* 8N1, FIFO enabled */
    uart_reg(UART_LCR_H) = UART_LCR_WLEN8 | UART_LCR_FEN;

    /* Set FIFO trigger level: RX interrupt at 1/8 full (2 bytes).
     * This gives the IRQ handler maximum time to drain. */
    uart_reg(UART_IFLS) = 0;  /* RX 1/8, TX 1/8 */

    /* Enable UART, TX, RX */
    uart_reg(UART_CR) = UART_CR_UARTEN | UART_CR_TXE | UART_CR_RXE;

    /* Clear all pending interrupts */
    uart_reg(UART_ICR) = 0x7FF;

    /* Enable RX + RX timeout interrupts */
    uart_reg(UART_IMSC) = UART_INT_RX | UART_INT_RT;

    /* Set up GIC for UART IRQ */
    gic_init_uart();
}

void uart_set_baud(uint32_t baud) {
    /* Disable RX interrupt during reconfiguration */
    uart_reg(UART_IMSC) = 0;

    while (!(uart_reg(UART_FR) & UART_FR_TXFE)) {}
    while (uart_reg(UART_FR) & UART_FR_BUSY) {}

    uart_reg(UART_CR) = 0;

    uint32_t divisor = UART_CLOCK / (16 * baud);
    uint32_t remainder = UART_CLOCK % (16 * baud);
    uint32_t frac = (remainder * 64 + (16 * baud) / 2) / (16 * baud);
    uart_reg(UART_IBRD) = divisor;
    uart_reg(UART_FBRD) = frac & 0x3F;

    uart_reg(UART_LCR_H) = UART_LCR_WLEN8 | UART_LCR_FEN;
    uart_reg(UART_IFLS) = 0;
    uart_reg(UART_CR) = UART_CR_UARTEN | UART_CR_TXE | UART_CR_RXE;
    uart_reg(UART_ICR) = 0x7FF;

    /* Re-enable RX interrupts */
    uart_reg(UART_IMSC) = UART_INT_RX | UART_INT_RT;

    /* Reset ring buffer (stale data from old baud rate) */
    rx_head = 0;
    rx_tail = 0;
}

void uart_putc(uint8_t ch) {
    volatile uint32_t timeout = 100000;
    while ((uart_reg(UART_FR) & UART_FR_TXFF) && timeout > 0) { timeout--; }
    uart_reg(UART_DR) = ch;
}

int uart_putc_safe(uint8_t ch) {
    volatile uint32_t timeout = 100000;
    while ((uart_reg(UART_FR) & UART_FR_TXFF) && timeout > 0) { timeout--; }
    if (timeout == 0) return -1;
    uart_reg(UART_DR) = ch;
    return 0;
}

uint8_t uart_getc(void) {
    /* Wait for data in ring buffer (filled by IRQ) */
    while (rx_head == rx_tail) {}
    uint8_t b = rx_buf[rx_tail];
    rx_tail = (rx_tail + 1) & RX_BUF_MASK;
    return b;
}

int uart_getc_safe(void) {
    /* Non-blocking read from ring buffer */
    if (rx_head == rx_tail) return -1;
    uint8_t b = rx_buf[rx_tail];
    rx_tail = (rx_tail + 1) & RX_BUF_MASK;
    return (int)b;
}

int uart_readable(void) {
    return rx_head != rx_tail;
}

void uart_clear_errors(void) {
    uart_reg(UART_RSR) = 0;
}

void uart_drain_rx(void) {
    /* Drain hardware FIFO */
    while (!(uart_reg(UART_FR) & UART_FR_RXFE)) {
        (void)uart_reg(UART_DR);
    }
    uart_reg(UART_RSR) = 0;
    /* Reset ring buffer */
    rx_head = 0;
    rx_tail = 0;
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
    uint32_t deadline = timeout_ms * 100;

    while (count < max_len) {
        if (uart_readable()) {
            buf[count++] = uart_getc();
            deadline = timeout_ms * 100;
        } else {
            if (deadline == 0 && count > 0) break;
            if (deadline > 0) deadline--;
            delay_us(10);
        }
    }
    return count;
}
