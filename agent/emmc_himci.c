/*
 * Minimal eMMC reader for hi3516cv500-family.
 *
 * Synopsys DesignWare MMC host controller @ EMMC_BASE = 0x10100000.
 * Reference: OpenIPC/openhisilicon bootrom/hi3516av300/re/bootloader.c
 * (initialize_emmc, configure_emmc_pins, update_emmc_card_clock,
 * memcpy_emmc). Card-side flow is standard eMMC 5.x identification.
 *
 * Scope: read-only MVP. CMD0/1/2/3/9/7 to identify, CMD17 single-block
 * read with FIFO drain. No partition switch, no erase, no write.
 *
 * The bootrom's PERISTAT-driven mode selection is bypassed — that path
 * returns -1 when PERISTAT[9:8] == 0 (the state the chip lands in after
 * UART fastboot + our vendor SPL). Instead we hardcode the mode-1 pinmux
 * values and a conservative clock divider; the eMMC card itself drops
 * to backwards-compatible 26 MHz default-speed mode on CMD7.
 */
#include "emmc_himci.h"

#ifdef EMMC_BASE

#include <stddef.h>
#include "uart.h"

/* ------------------------------------------------------------------------ */
/* DesignWare DW MMC controller register offsets (relative to EMMC_BASE).   */
/* ------------------------------------------------------------------------ */
#define DW_CTRL         0x000   /* control: reset bits[2:0] = DMA/FIFO/INT */
#define DW_PWREN        0x004
#define DW_CLKDIV       0x008
#define DW_CLKSRC       0x00c
#define DW_CLKENA       0x010
#define DW_TMOUT        0x014
#define DW_CTYPE        0x018   /* card type: bit0=4-bit, bit16=8-bit */
#define DW_BLKSIZ       0x01c
#define DW_BYTCNT       0x020
#define DW_INTMASK      0x024
#define DW_CMDARG       0x028
#define DW_CMD          0x02c
#define DW_RESP0        0x030
#define DW_RESP1        0x034
#define DW_RESP2        0x038
#define DW_RESP3        0x03c
#define DW_MINTSTS      0x040
#define DW_RINTSTS      0x044
#define DW_STATUS       0x048
#define DW_FIFOTH       0x04c
#define DW_TCBCNT       0x05c
#define DW_TBBCNT       0x060
#define DW_USRID        0x068
#define DW_CARD_RSTN    0x078
#define DW_FIFO         0x200

/* CTRL bits */
#define CTRL_CONTROLLER_RESET   (1u << 0)
#define CTRL_FIFO_RESET         (1u << 1)
#define CTRL_DMA_RESET          (1u << 2)
#define CTRL_INT_ENABLE         (1u << 4)
#define CTRL_RESET_ALL          (CTRL_CONTROLLER_RESET | CTRL_FIFO_RESET | CTRL_DMA_RESET)

/* CMD bits — used to drive the card; bit 31 = start_cmd ("send this") */
#define CMD_START               (1u << 31)
#define CMD_USE_HOLD_REG        (1u << 29)
#define CMD_UPDATE_CLK_REGS     (1u << 21)    /* clock-update barrier */
#define CMD_SEND_INITIALIZATION (1u << 15)
#define CMD_STOP_ABORT          (1u << 14)
#define CMD_WAIT_PRVDATA        (1u << 13)
#define CMD_SEND_AUTO_STOP      (1u << 12)
#define CMD_DATA_EXPECTED       (1u << 9)
#define CMD_RESP_LONG           (1u << 7)
#define CMD_RESP_CHK_CRC        (1u << 8)
#define CMD_RESP_EXPECTED       (1u << 6)
#define CMD_INDEX(n)            ((n) & 0x3F)

/* RINTSTS bits */
#define RINTSTS_RTO             (1u << 8)     /* response timeout */
#define RINTSTS_CMD_DONE        (1u << 2)     /* command done */
#define RINTSTS_RESP_ERR        (1u << 1)
#define RINTSTS_RCRC_ERR        (1u << 6)
#define RINTSTS_DCRC_ERR        (1u << 7)
#define RINTSTS_DTO             (1u << 3)     /* data transfer over */
#define RINTSTS_DRTO            (1u << 9)
#define RINTSTS_SBE             (1u << 13)
#define RINTSTS_EBE             (1u << 15)
#define RINTSTS_HLE             (1u << 12)
#define RINTSTS_ERROR_MASK      (RINTSTS_RTO | RINTSTS_RESP_ERR | \
                                 RINTSTS_RCRC_ERR | RINTSTS_DCRC_ERR | \
                                 RINTSTS_DRTO | RINTSTS_SBE | RINTSTS_EBE | \
                                 RINTSTS_HLE)

/* STATUS bits */
#define STATUS_FIFO_COUNT_SHIFT 17
#define STATUS_FIFO_COUNT_MASK  0x1FFF
#define STATUS_DATA_BUSY        (1u << 9)
#define STATUS_FIFO_FULL        (1u << 3)
#define STATUS_FIFO_EMPTY       (1u << 2)

/* ------------------------------------------------------------------------ */
/* eMMC card commands (subset).                                             */
/* ------------------------------------------------------------------------ */
#define MMC_GO_IDLE_STATE       0
#define MMC_SEND_OP_COND        1
#define MMC_ALL_SEND_CID        2
#define MMC_SET_RELATIVE_ADDR   3
#define MMC_SEND_CSD            9
#define MMC_SEND_CID            10
#define MMC_SELECT_CARD         7
#define MMC_READ_SINGLE_BLOCK   17

/* Per JEDEC, OCR bit 30 = high-capacity; we always ask for high-capacity
 * support along with the full 3.3V voltage window. */
#define OCR_VOLTAGE_MASK        0x00FF8000u
#define OCR_HC_SECTOR           0x40000000u
#define OCR_BUSY                0x80000000u

/* ------------------------------------------------------------------------ */
/* Hardcoded pinmux for av300/dv300/cv500-family. Bootrom table at          */
/* 0x04007d98 holds 32 dwords; this is the slice that maps the eMMC bus     */
/* with conservative pull configuration. Mode-1 row (4-bit MMC, default     */
/* speed) per the bootrom's `table[mode + 0..6]` indexing.                  */
/* ------------------------------------------------------------------------ */
static const uint32_t emmc_pinmux[7] = {
    0x000006c0,  /* iocfg0[0] */
    0x000006b0,  /* iocfg0[1] */
    0x00000000,  /* iocfg0[2] */
    0x000005f0,  /* iocfg0[3] */
    0x000005f0,  /* iocfg0[4] */
    0x000005e0,  /* iocfg0[5] */
    0x00000000,  /* iocfg0[6] */
};

/* ------------------------------------------------------------------------ */
/* Globals (referenced by the agent's CMD_INFO / CMD_READ handlers).        */
/* ------------------------------------------------------------------------ */
uint64_t emmc_capacity_bytes = 0;
uint8_t  emmc_cid[16] = {0};
static uint16_t emmc_rca = 0;

/* ------------------------------------------------------------------------ */
/* Low-level helpers.                                                       */
/* ------------------------------------------------------------------------ */
static inline volatile uint32_t *emmc_reg(uint32_t off) {
    return (volatile uint32_t *)(EMMC_BASE + off);
}

static inline volatile uint32_t *iocfg0_reg(uint32_t idx) {
    return (volatile uint32_t *)(IO_CTRL0_BASE + idx * 4);
}

static inline volatile uint32_t *crg_reg(uint32_t off) {
    return (volatile uint32_t *)(CRG_BASE + off);
}

static void busy_wait(uint32_t cycles) {
    /* Crude spin — used for short settling delays. */
    volatile uint32_t c = cycles;
    while (c--) { __asm__ volatile ("" ::: "memory"); }
}

/* Issue a CMD-register clock-update handshake. Per the bootrom RE this is
 * a DWMMC pattern, not a real card command — controller commits pending
 * CLKDIV/CLKENA changes when CMD_UPDATE_CLK_REGS is asserted. */
static int emmc_update_clk(void) {
    int retries = 3;
    while (retries--) {
        *emmc_reg(DW_CMD) = CMD_START | CMD_USE_HOLD_REG | CMD_UPDATE_CLK_REGS
                          | CMD_WAIT_PRVDATA;
        uint32_t tries = 0xF00;
        while (tries--) {
            if (!(*emmc_reg(DW_CMD) & CMD_START))
                return 0;
            if (*emmc_reg(DW_RINTSTS) & RINTSTS_HLE)
                break;  /* hardware locked — outer retry */
        }
    }
    return -1;
}

/* Send a card command. `cmd_idx` is the MMC CMDn number, `arg` the
 * 32-bit argument, `flags` extra CMD bits (DATA_EXPECTED, RESP_LONG,
 * NO_CRC for OCR/CID-style responses that don't have a CRC).
 *
 * Bit policy mirrors the Linux dw_mmc driver: response-expected commands
 * also enable CHK_CRC by default (subset of cards reply with no CRC, which
 * we mark via a NO_CRC sentinel flag in `flags`); data-bearing commands
 * set WAIT_PRVDATA so the controller queues them after any in-flight DMA.
 *
 * Returns 0 on CMD_DONE, negative on RTO/error/timeout. */
#define EMMC_FLAG_NO_CRC  (1u << 31)   /* tells us NOT to set CHK_CRC */

static int emmc_send_cmd(uint32_t cmd_idx, uint32_t arg, uint32_t flags) {
    /* Clear stale interrupts. */
    *emmc_reg(DW_RINTSTS) = 0xFFFFFFFFu;
    *emmc_reg(DW_CMDARG)  = arg;

    uint32_t cmd = CMD_START | CMD_USE_HOLD_REG | CMD_INDEX(cmd_idx)
                 | (flags & ~EMMC_FLAG_NO_CRC);
    if ((flags & CMD_RESP_EXPECTED) && !(flags & EMMC_FLAG_NO_CRC))
        cmd |= CMD_RESP_CHK_CRC;
    if (flags & CMD_DATA_EXPECTED)
        cmd |= CMD_WAIT_PRVDATA;
    *emmc_reg(DW_CMD) = cmd;

    /* Spin for command-done. 5M cycles at ~1 GHz is ~5 ms, ample for any
     * identification CMD; for data commands the CMD_DONE fires after the
     * response (well before the data has finished). */
    uint32_t spin = 5000000;
    while (spin--) {
        uint32_t stat = *emmc_reg(DW_RINTSTS);
        if (stat & RINTSTS_ERROR_MASK)
            return -1;
        if (stat & RINTSTS_CMD_DONE)
            return 0;
    }
    return -1;
}

/* Read FIFO word — caller must ensure data is available. */
static inline uint32_t emmc_fifo_read(void) {
    return *emmc_reg(DW_FIFO);
}

/* ------------------------------------------------------------------------ */
/* Controller / card bring-up.                                              */
/* ------------------------------------------------------------------------ */

static void emmc_pinmux_setup(void) {
    for (int i = 0; i < 7; i++)
        *iocfg0_reg(i) = emmc_pinmux[i];
}

static void emmc_crg_setup(void) {
    /* CRG[0x148] is the eMMC clock/reset register.
     * bit 0 = soft reset (1 = asserted)
     * bits[3:1] = clock mux/enable bits (vendor mode-1 = 0xe == 0b1110)
     *
     * Sequence: assert reset, settle, deassert + enable clock. */
    volatile uint32_t *c148 = crg_reg(0x148);
    uint32_t v = *c148;
    *c148 = v | 1u;            /* reset */
    busy_wait(100000);
    v &= ~0x0Fu;
    v |= 0x0Eu;                /* clock enable, take out of reset */
    *c148 = v;
}

static int emmc_controller_reset(void) {
    /* Full reset of DMA/FIFO/INT. Poll until controller clears them. */
    *emmc_reg(DW_CTRL) = CTRL_RESET_ALL;
    uint32_t spin = 100000;
    while ((*emmc_reg(DW_CTRL) & CTRL_RESET_ALL) && spin--) {}
    if (*emmc_reg(DW_CTRL) & CTRL_RESET_ALL)
        return -1;

    /* Default register state for PIO mode. */
    *emmc_reg(DW_CMDARG)  = 0;
    *emmc_reg(DW_RINTSTS) = 0xFFFFFFFFu;
    *emmc_reg(DW_TBBCNT)  = 0xFFFFFFFFu;
    *emmc_reg(DW_CTRL)    = CTRL_INT_ENABLE;
    *emmc_reg(DW_INTMASK) = 0;
    *emmc_reg(DW_TMOUT)   = 0xFFFFFF40u;
    *emmc_reg(DW_BLKSIZ)  = 0x200;          /* 512 byte blocks */
    *emmc_reg(DW_BYTCNT)  = 0x200;
    *emmc_reg(DW_FIFOTH)  = (1u << 28) | (0x07Fu << 16);  /* rx=128, tx=128 */
    /* CTYPE = 0 = 1-bit. The card is in 1-bit mode after power-on until
     * CMD6 (SWITCH) widens it; for MVP we stay at 1-bit to avoid the
     * bus-width-mismatch that wedges the data path. Slower, but works. */
    *emmc_reg(DW_CTYPE)   = 0;
    return 0;
}

static int emmc_setup_clock(uint32_t divider) {
    /* DWMMC clock-change protocol: disable clock, set divider, enable. */
    *emmc_reg(DW_CLKENA) = 0;
    if (emmc_update_clk() < 0) return -1;

    *emmc_reg(DW_CLKDIV) = divider;
    *emmc_reg(DW_CLKSRC) = 0;
    if (emmc_update_clk() < 0) return -1;

    *emmc_reg(DW_CLKENA) = 1;
    if (emmc_update_clk() < 0) return -1;
    return 0;
}

static int emmc_card_identify(void) {
    /* CMD0: GO_IDLE_STATE — no response. */
    *emmc_reg(DW_RINTSTS) = 0xFFFFFFFFu;
    *emmc_reg(DW_CMDARG)  = 0;
    *emmc_reg(DW_CMD) = CMD_START | CMD_USE_HOLD_REG
                     | CMD_SEND_INITIALIZATION
                     | CMD_INDEX(MMC_GO_IDLE_STATE);
    uint32_t spin = 5000000;
    while (spin-- && !(*emmc_reg(DW_RINTSTS) & RINTSTS_CMD_DONE)) {}
    if (!(*emmc_reg(DW_RINTSTS) & RINTSTS_CMD_DONE)) return -1;
    busy_wait(50000);

    /* CMD1: SEND_OP_COND — repeat until the card finishes its power-up
     * (OCR busy bit set). Ask for HC sector mode + full 3.3V window.
     * OCR (R3) has no CRC, so suppress CHK_CRC. */
    uint32_t ocr = 0;
    for (int attempt = 0; attempt < 1000; attempt++) {
        if (emmc_send_cmd(MMC_SEND_OP_COND,
                          OCR_HC_SECTOR | OCR_VOLTAGE_MASK,
                          CMD_RESP_EXPECTED | EMMC_FLAG_NO_CRC) < 0)
            return -2;
        ocr = *emmc_reg(DW_RESP0);
        if (ocr & OCR_BUSY) break;
        busy_wait(50000);
    }
    if (!(ocr & OCR_BUSY)) return -3;
    int hc = !!(ocr & OCR_HC_SECTOR);

    /* CMD2: ALL_SEND_CID — long response, no CRC check (always passes). */
    if (emmc_send_cmd(MMC_ALL_SEND_CID, 0,
                      CMD_RESP_EXPECTED | CMD_RESP_LONG | EMMC_FLAG_NO_CRC) < 0)
        return -4;
    /* DWMMC R2 response (128 bits) is mapped:
     *   RESP3 (0x3c) = response[127:96]   ← MSB / MID lives here
     *   RESP2 (0x38) = response[95:64]
     *   RESP1 (0x34) = response[63:32]
     *   RESP0 (0x30) = response[31:0]     ← LSB
     * The controller drops the 8-bit CRC, so what we read is CID[127:0]. */
    uint32_t r0 = *emmc_reg(DW_RESP0);
    uint32_t r1 = *emmc_reg(DW_RESP1);
    uint32_t r2 = *emmc_reg(DW_RESP2);
    uint32_t r3 = *emmc_reg(DW_RESP3);
    emmc_cid[0]  = (r3 >> 24) & 0xFF;  /* MID */
    emmc_cid[1]  = (r3 >> 16) & 0xFF;
    emmc_cid[2]  = (r3 >>  8) & 0xFF;
    emmc_cid[3]  = (r3 >>  0) & 0xFF;
    emmc_cid[4]  = (r2 >> 24) & 0xFF;
    emmc_cid[5]  = (r2 >> 16) & 0xFF;
    emmc_cid[6]  = (r2 >>  8) & 0xFF;
    emmc_cid[7]  = (r2 >>  0) & 0xFF;
    emmc_cid[8]  = (r1 >> 24) & 0xFF;
    emmc_cid[9]  = (r1 >> 16) & 0xFF;
    emmc_cid[10] = (r1 >>  8) & 0xFF;
    emmc_cid[11] = (r1 >>  0) & 0xFF;
    emmc_cid[12] = (r0 >> 24) & 0xFF;
    emmc_cid[13] = (r0 >> 16) & 0xFF;
    emmc_cid[14] = (r0 >>  8) & 0xFF;
    emmc_cid[15] = (r0 >>  0) & 0xFF;

    /* CMD3: SET_RELATIVE_ADDR — assign RCA. We pick 0x0001 to keep it
     * simple; subsequent addressed commands use this in the high 16 bits
     * of CMDARG. */
    emmc_rca = 0x0001;
    if (emmc_send_cmd(MMC_SET_RELATIVE_ADDR,
                      (uint32_t)emmc_rca << 16,
                      CMD_RESP_EXPECTED) < 0)
        return -5;

    /* CMD9: SEND_CSD — long response. Parse capacity. For HC cards the
     * EXT_CSD's SEC_COUNT is the proper source; CSD-side C_SIZE = 0xFFFFFF
     * sentinel. For SC cards C_SIZE in CSD[73:62] determines size. defib's
     * eMMC targets are ≥ 2 GiB so we read CSD CCC and SECTOR_COUNT bits. */
    if (emmc_send_cmd(MMC_SEND_CSD,
                      (uint32_t)emmc_rca << 16,
                      CMD_RESP_EXPECTED | CMD_RESP_LONG | EMMC_FLAG_NO_CRC) < 0)
        return -6;
    if (hc) {
        /* eMMC ≥ 2 GiB cards encode true capacity in EXT_CSD SEC_COUNT.
         * Reading EXT_CSD needs a data-transfer command (CMD8 SEND_EXT_CSD)
         * which the MVP doesn't implement.  Until that lands, cap at the
         * largest uint32_t-representable boundary so the addr_readable
         * window covers everything an eMMC partition table is likely to
         * reference (4 GiB - 1 sector). Host can read up to that limit;
         * truly accurate capacity reporting + access > 4 GiB is follow-up. */
        emmc_capacity_bytes = 0xFFFFFE00ull;
    } else {
        /* SC (≤ 2 GiB): CSD has 12-bit C_SIZE at bits[73:62].  Standard
         * formula: (C_SIZE+1) * 2^(C_SIZE_MULT+2) * 2^READ_BL_LEN. */
        uint32_t csd1 = *emmc_reg(DW_RESP1);
        uint32_t csd2 = *emmc_reg(DW_RESP2);
        uint32_t csize = ((csd1 & 0x000003FFu) << 2)
                       | ((csd2 >> 30) & 0x3u);
        uint32_t mult  = (csd1 >> 15) & 0x7u;
        uint32_t rblen = (csd2 >> 16) & 0xFu;
        if (rblen >= 9 && rblen <= 12) {
            uint32_t blocknr = (csize + 1) << (mult + 2);
            emmc_capacity_bytes = (uint64_t)blocknr << rblen;
        } else {
            emmc_capacity_bytes = 0;
        }
    }

    /* CMD7: SELECT_CARD — move card to TRAN state. */
    if (emmc_send_cmd(MMC_SELECT_CARD,
                      (uint32_t)emmc_rca << 16,
                      CMD_RESP_EXPECTED) < 0)
        return -7;
    return 0;
}

int emmc_init(void) {
    emmc_pinmux_setup();
    emmc_crg_setup();
    busy_wait(100000);

    if (emmc_controller_reset() < 0) return -10;

    /* Init clock divider: 400 kHz-ish for ID phase. The DWMMC clock is
     * sourced at a frequency the bootrom programmed; without knowing it
     * exactly we err on the safe side. Divider=128 gives a comfortable
     * margin during identification; afterwards eMMC card defaults to
     * 26 MHz on the bus until we issue CMD6 to switch — we don't, so
     * the slow divisor stays in effect. Throughput at ~10 KB/s/block
     * is plenty for an MVP. */
    if (emmc_setup_clock(0x80) < 0) return -11;

    /* CARD_RSTN reset cycle: pulse the card's hardware reset before
     * starting the identification. */
    *emmc_reg(DW_CARD_RSTN) = 0;
    busy_wait(100000);
    *emmc_reg(DW_CARD_RSTN) = 1;
    busy_wait(1000000);   /* card boot delay */

    return emmc_card_identify();
}

/* ------------------------------------------------------------------------ */
/* Read.                                                                    */
/* ------------------------------------------------------------------------ */

int emmc_read_block(uint32_t block_no, uint8_t *dst) {
    if (emmc_capacity_bytes == 0)
        return -1;

    /* Wait for card to be out of data busy (DAT0 line high). */
    uint32_t spin = 5000000;
    while ((*emmc_reg(DW_STATUS) & STATUS_DATA_BUSY) && spin--) {}
    if (*emmc_reg(DW_STATUS) & STATUS_DATA_BUSY) return -2;

    /* Reset FIFO before the read so we start from a clean state. */
    *emmc_reg(DW_CTRL) |= CTRL_FIFO_RESET;
    spin = 100000;
    while ((*emmc_reg(DW_CTRL) & CTRL_FIFO_RESET) && spin--) {}

    *emmc_reg(DW_BLKSIZ)  = 0x200;
    *emmc_reg(DW_BYTCNT)  = 0x200;
    *emmc_reg(DW_RINTSTS) = 0xFFFFFFFFu;

    /* CMD17: READ_SINGLE_BLOCK. Argument is block number (HC) or byte
     * offset (SC). emmc_capacity_bytes parsing assumed HC. */
    if (emmc_send_cmd(MMC_READ_SINGLE_BLOCK, block_no,
                      CMD_RESP_EXPECTED | CMD_DATA_EXPECTED) < 0)
        return -3;

    /* Drain 128 words (512 bytes) from the FIFO, polling FIFO_COUNT. */
    uint32_t words_left = 128;
    uint8_t *out = dst;
    spin = 50000000;   /* very generous — slow clocks during MVP */
    while (words_left && spin--) {
        uint32_t status = *emmc_reg(DW_STATUS);
        uint32_t avail = (status >> STATUS_FIFO_COUNT_SHIFT)
                       & STATUS_FIFO_COUNT_MASK;
        if (avail == 0) {
            if (*emmc_reg(DW_RINTSTS) & RINTSTS_ERROR_MASK)
                return -4;
            continue;
        }
        if (avail > words_left) avail = words_left;
        for (uint32_t i = 0; i < avail; i++) {
            uint32_t w = emmc_fifo_read();
            *out++ = (w >>  0) & 0xFF;
            *out++ = (w >>  8) & 0xFF;
            *out++ = (w >> 16) & 0xFF;
            *out++ = (w >> 24) & 0xFF;
        }
        words_left -= avail;
    }
    if (words_left) return -5;

    /* Wait for the DTO interrupt to confirm the block is fully delivered. */
    spin = 5000000;
    while (spin--) {
        uint32_t stat = *emmc_reg(DW_RINTSTS);
        if (stat & RINTSTS_ERROR_MASK) return -6;
        if (stat & RINTSTS_DTO) return 0;
    }
    return -7;
}

#endif  /* EMMC_BASE */
