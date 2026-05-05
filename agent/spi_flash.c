/*
 * HiSilicon FMC SPI NOR flash driver.
 *
 * Read: via memory-mapped window at FLASH_MEM (zero overhead).
 * Write/Erase: via FMC register interface.
 *
 * Register definitions from U-Boot hifmc100 driver and
 * qemu-hisilicon/qemu/hw/misc/hisi-fmc.c
 */

#include "spi_flash.h"
#include "protocol.h"  /* for crc32() */

/* FMC register offsets */
#define FMC_CFG             0x00
#define FMC_GLOBAL_CFG      0x04
#define FMC_SPI_TIMING_CFG  0x08
#define FMC_INT             0x18
#define FMC_INT_CLR         0x20
#define FMC_CMD             0x24
#define FMC_ADDRH           0x28
#define FMC_ADDRL           0x2C
#define FMC_OP_CFG          0x30
#define FMC_DATA_NUM        0x38
#define FMC_OP              0x3C
#define FMC_DMA_LEN         0x40
#define FMC_DMA_SADDR_D0    0x4C
#define FMC_DMA_SADDR_OOB   0x5C
#define FMC_OP_CTRL         0x68
#define FMC_STATUS          0xAC
#define FMC_VERSION         0xBC

/* FMC_CFG bits */
#define FMC_CFG_OP_MODE_NORMAL  (1 << 0)
#define FMC_CFG_FLASH_SEL_NOR   (0 << 1)
#define FMC_CFG_FLASH_SEL_NAND  (1 << 1)
#define FMC_CFG_FLASH_SEL_MASK  (3 << 1)

/* FMC_OP_CFG bits */
#define OP_CFG_OEN_EN       (1 << 13)
#define OP_CFG_FM_CS(n)     ((n) << 11)
#define OP_CFG_CS(n)        ((n) << 11)
#define OP_CFG_ADDR_NUM(n)  ((n) << 4)  /* 3 or 4 byte address */
#define OP_CFG_DUMMY_NUM(n) ((n) & 0xF) /* dummy bytes between address and data */
#define OP_CFG_MEM_IF_TYPE(t) (((t) & 0x7) << 7)

/* FMC_OP_CTRL bits — used by the NAND-aware page-read/program flow.
 * Unlike FMC_OP (which fits classic NOR-style cmd+addr+data), OP_CTRL
 * lets the FMC sequence PAGE_READ + READ_FROM_CACHE (or PROGRAM_LOAD +
 * PROGRAM_EXECUTE) internally — including the chip's post-address
 * dummy timing — and stream a full 2 KiB page via DMA. */
#define OP_CTRL_RD_OPCODE(c)  (((c) & 0xff) << 16)
#define OP_CTRL_WR_OPCODE(c)  (((c) & 0xff) << 8)
#define OP_CTRL_RD_OP_SEL(o)  (((o) & 0x3) << 4)
#define OP_CTRL_DMA_OP(t)     ((t) << 2)
#define OP_CTRL_RW_OP(r)      ((r) << 1)
#define OP_CTRL_DMA_OP_READY  1
#define RD_OP_READ_ALL_PAGE   0
#define OP_TYPE_DMA           0
#define OP_TYPE_REG           1
#define RW_OP_READ            0
#define RW_OP_WRITE           1

/* FMC_INT bits */
#define FMC_INT_OP_DONE  (1 << 0)

/* FMC_OP bits */
#define FMC_OP_REG_OP_START (1 << 0)
#define FMC_OP_READ_STATUS  (1 << 1)
#define FMC_OP_READ_DATA    (1 << 2)
#define FMC_OP_WRITE_DATA   (1 << 5)
#define FMC_OP_ADDR_EN      (1 << 6)
#define FMC_OP_CMD1_EN      (1 << 7)

/* SPI NOR commands */
#define SPI_CMD_READ_ID       0x9F
#define SPI_CMD_READ_STATUS   0x05
#define SPI_CMD_WRITE_ENABLE  0x06
#define SPI_CMD_PAGE_PROGRAM  0x02
#define SPI_CMD_SECTOR_ERASE  0xD8
#define SPI_CMD_CHIP_ERASE    0xC7

/* SPI NOR status bits */
#define SPI_STATUS_WIP  (1 << 0)
#define SPI_STATUS_WEL  (1 << 1)

/* SPI NAND commands (MX35LF1GE4AB / generic SPI NAND) */
#define SPI_CMD_NAND_GET_FEATURES   0x0F
#define SPI_CMD_NAND_SET_FEATURE    0x1F
#define SPI_CMD_NAND_PAGE_READ      0x13   /* row addr -> chip cache */
#define SPI_CMD_NAND_READ_CACHE     0x03   /* col addr -> read cached page */
#define SPI_CMD_NAND_PROGRAM_LOAD   0x02   /* col addr -> load data, RESETS cache */
#define SPI_CMD_NAND_PROGRAM_RAND   0x84   /* col addr -> load data, keeps cache */
#define SPI_CMD_NAND_PROGRAM_EXEC   0x10   /* row addr -> commit cache to NAND */
#define SPI_CMD_NAND_BLOCK_ERASE    0xD8   /* row addr -> erase 128 KiB block */

/* SPI NAND feature register addresses */
#define NAND_FEATURE_PROTECT  0xA0
#define NAND_FEATURE_OTP      0xB0       /* bit 4 = ECC_E (on-chip ECC enable) */
#define NAND_FEATURE_STATUS   0xC0       /* OIP, WEL, E_FAIL, P_FAIL, ECC_S */

/* SPI NAND status bits (feature 0xC0) */
#define NAND_STATUS_OIP     (1 << 0)     /* Operation In Progress */
#define NAND_STATUS_WEL     (1 << 1)     /* Write Enable Latch */
#define NAND_STATUS_E_FAIL  (1 << 2)     /* Erase Fail */
#define NAND_STATUS_P_FAIL  (1 << 3)     /* Program Fail */

/* NAND geometry — currently only MX35LF1GE4AB (1Gbit) is recognized.
 * On-chip ECC is enabled by default; reads return ECC-corrected data. */
#define NAND_PAGE_SIZE   2048
#define NAND_BLOCK_SIZE  (64 * NAND_PAGE_SIZE)   /* 128 KiB */

/* CRG register for FMC clock — CRG_BASE is per-SoC (set via -DCRG_BASE=...) */
#define REG_FMC_CRG         (*(volatile uint32_t *)(CRG_BASE + 0x0144))
#define FMC_CLK_ENABLE      (1 << 1)
#define FMC_SOFT_RESET      (1 << 0)

/* SPI timing: TCSH=6 [15:12], TCSS=6 [11:8], TSHSL=0xF [7:0] */
#define SPI_TIMING_VAL      ((6 << 12) | (6 << 8) | 0xF)  /* 0x660F */

/* Flash size from JEDEC ID byte 2 */
static uint32_t detect_size(uint8_t id2) {
    if (id2 >= 0x14 && id2 <= 0x1A)
        return 1u << id2;
    return 0x1000000; /* Default 16MB */
}

/* Forward declarations */
static void fmc_wait_ready(void);
static void spi_wait_wip(void);

/* Mode switching: normal mode for register commands, boot mode for reads */
/* I/O pad configuration base for SPI flash pins */
#define IO_BASE  0x100C0000
#define io_reg(off) (*(volatile uint32_t *)(IO_BASE + (off)))

static void fmc_enter_normal(void) {
    /* Full FMC init for register-mode operations (matching U-Boot) */

    /* Configure SPI flash I/O pads (SPL may have left them in boot-mode config) */
    io_reg(0x14) = 0x401;  /* sfc_clk */
    io_reg(0x18) = 0x461;  /* sfc_hold_io0 */
    io_reg(0x1C) = 0x461;  /* sfc_miso_io1 */
    io_reg(0x20) = 0x461;  /* sfc_wp_io2 */
    io_reg(0x24) = 0x461;  /* sfc_mosi_io3 */
    io_reg(0x28) = 0x461;  /* sfc_csn */

    fmc_reg(FMC_CFG) = 0x1821;  /* OP_MODE_NORMAL | bootrom defaults */
    fmc_reg(FMC_SPI_TIMING_CFG) = SPI_TIMING_VAL;
    fmc_reg(FMC_INT_CLR) = 0xFF;
    fmc_reg(FMC_GLOBAL_CFG) = 0;  /* Disable all write protection */
}

static void fmc_enter_boot(void) {
    /* FMC_CFG bit 0 can't be cleared by writing — once in normal mode,
     * the only way back to boot mode is a soft reset via CRG register.
     * This resets the FMC controller state, restoring boot mode. */
    fmc_wait_ready();
    spi_wait_wip();

    /* Soft reset: set bit 0, then clear it */
    uint32_t crg = REG_FMC_CRG;
    REG_FMC_CRG = crg | FMC_SOFT_RESET;
    /* Brief delay for reset to take effect */
    for (volatile int i = 0; i < 1000; i++) {}
    REG_FMC_CRG = crg & ~FMC_SOFT_RESET;
    for (volatile int i = 0; i < 1000; i++) {}

    /* Soft reset clears FMC registers — reconfigure for proper boot mode.
     * Without this, the memory window at FLASH_MEM wraps at 1MB. */
    fmc_reg(FMC_SPI_TIMING_CFG) = SPI_TIMING_VAL;
    fmc_reg(FMC_INT_CLR) = 0xFF;
}

static void fmc_wait_ready(void) {
    volatile uint32_t timeout = 400000;
    while ((fmc_reg(FMC_OP) & FMC_OP_REG_OP_START) && timeout > 0)
        timeout--;
}

static void spi_wait_wip(void) {
    for (int i = 0; i < 10000000; i++) {
        uint8_t sr = flash_read_status();
        if (!(sr & SPI_STATUS_WIP)) return;
        /* Drain UART FIFO while waiting — prevents overflow during
         * long flash operations (erase ~150ms, page program ~1-3ms) */
        proto_drain_fifo();
    }
}

static void spi_write_enable(void) {
    /* Ensure hardware WP is deasserted before write enable.
     * FMC_GLOBAL_CFG bit 2 = WP_ENABLE, bit 6 = WP_LEVEL.
     * Clear both to fully disable write protection. */
    fmc_reg(FMC_GLOBAL_CFG) = 0;

    fmc_reg(FMC_CMD) = SPI_CMD_WRITE_ENABLE;
    fmc_reg(FMC_OP_CFG) = OP_CFG_OEN_EN | OP_CFG_CS(0);
    fmc_reg(FMC_OP) = FMC_OP_CMD1_EN | FMC_OP_REG_OP_START;
    fmc_wait_ready();
}

/*
 * Clear block protection bits in the flash status register.
 * HiSilicon SPL locks all sectors (BP0-BP3 = 0x38 in status register).
 * Must be called before any erase/write operation.
 */
#define SPI_CMD_WRITE_STATUS  0x01
#define SPI_STATUS_BP_MASK    0x7C  /* BP0-BP4: bits 2-6 */

static void flash_unlock(void) {
    /* Read current status */
    uint8_t sr_before = flash_read_status();

    if (!(sr_before & SPI_STATUS_BP_MASK)) return;  /* Already unlocked */

    /* Disable write protect in FMC_GLOBAL_CFG (bit 2 = WP_ENABLE).
     * U-Boot does this before any status register write. */
    uint32_t gcfg = fmc_reg(FMC_GLOBAL_CFG);
    fmc_reg(FMC_GLOBAL_CFG) = gcfg & ~(1 << 2);

    /* Write enable required before status register write */
    spi_write_enable();

    /* Verify WEL is set */
    uint8_t sr_wel = flash_read_status();
    (void)sr_wel;  /* Available for debugging */

    /* Write status register = 0x00 (clear all BP bits) */
    volatile uint8_t *fmc_buf = (volatile uint8_t *)(FLASH_MEM);
    fmc_buf[0] = 0x00;

    fmc_reg(FMC_CMD) = SPI_CMD_WRITE_STATUS;
    fmc_reg(FMC_DATA_NUM) = 1;
    fmc_reg(FMC_OP_CFG) = OP_CFG_OEN_EN | OP_CFG_CS(0);
    fmc_reg(FMC_OP) = FMC_OP_CMD1_EN | FMC_OP_WRITE_DATA | FMC_OP_REG_OP_START;
    fmc_wait_ready();
    spi_wait_wip();

    /* Verify status cleared */
    uint8_t sr_after = flash_read_status();

    /* Store results for diagnostic (accessible via flash_info) */
    flash_unlock_debug[0] = sr_before;
    flash_unlock_debug[1] = sr_wel;
    flash_unlock_debug[2] = sr_after;
}

void flash_read_id(uint8_t id[3]) {
    fmc_reg(FMC_CMD) = SPI_CMD_READ_ID;
    fmc_reg(FMC_OP_CFG) = OP_CFG_OEN_EN | OP_CFG_CS(0);
    fmc_reg(FMC_DATA_NUM) = 8;
    fmc_reg(FMC_OP) = FMC_OP_CMD1_EN | FMC_OP_READ_DATA | FMC_OP_REG_OP_START;
    fmc_wait_ready();

    /* Read from memory window (data lands here on real hardware) */
    uint32_t data = *(volatile uint32_t *)(FLASH_MEM);
    id[0] = (data >> 0) & 0xFF;
    id[1] = (data >> 8) & 0xFF;
    id[2] = (data >> 16) & 0xFF;
}

/* Cached flash type from last flash_init — used by flash_read to dispatch. */
static uint8_t current_flash_type = FLASH_TYPE_NOR;

/* Read a single byte from a SPI NAND feature register (GET_FEATURES 0x0F). */
static uint8_t nand_get_feature(uint8_t addr) {
    fmc_reg(FMC_INT_CLR) = 0xFF;
    fmc_reg(FMC_CMD) = SPI_CMD_NAND_GET_FEATURES;
    fmc_reg(FMC_ADDRL) = addr;
    fmc_reg(FMC_OP_CFG) = OP_CFG_OEN_EN | OP_CFG_CS(0) | OP_CFG_ADDR_NUM(1);
    fmc_reg(FMC_DATA_NUM) = 1;
    fmc_reg(FMC_OP) = FMC_OP_CMD1_EN | FMC_OP_ADDR_EN | FMC_OP_READ_DATA | FMC_OP_REG_OP_START;
    fmc_wait_ready();
    return *(volatile uint8_t *)(FLASH_MEM);
}

/* Write a single byte to a SPI NAND feature register (SET_FEATURE 0x1F). */
static void nand_set_feature(uint8_t addr, uint8_t val) {
    *(volatile uint8_t *)(FLASH_MEM) = val;
    fmc_reg(FMC_INT_CLR) = 0xFF;
    fmc_reg(FMC_CMD) = SPI_CMD_NAND_SET_FEATURE;
    fmc_reg(FMC_ADDRL) = addr;
    fmc_reg(FMC_OP_CFG) = OP_CFG_OEN_EN | OP_CFG_CS(0) | OP_CFG_ADDR_NUM(1);
    fmc_reg(FMC_DATA_NUM) = 1;
    fmc_reg(FMC_OP) = FMC_OP_CMD1_EN | FMC_OP_ADDR_EN | FMC_OP_WRITE_DATA | FMC_OP_REG_OP_START;
    fmc_wait_ready();
}

/* Issue WRITE_ENABLE (0x06) — sets WEL in status. Required before erase/program. */
static void nand_write_enable(void) {
    fmc_reg(FMC_INT_CLR) = 0xFF;
    fmc_reg(FMC_CMD) = SPI_CMD_WRITE_ENABLE;
    fmc_reg(FMC_OP_CFG) = OP_CFG_OEN_EN | OP_CFG_CS(0);
    fmc_reg(FMC_OP) = FMC_OP_CMD1_EN | FMC_OP_REG_OP_START;
    fmc_wait_ready();
}

/* Identify SPI NAND chip from JEDEC ID. Returns 1 if recognized, 0 otherwise.
 * Currently only MX35LF1GE4AB (Macronix, c2 12, 1Gbit / 128MB).  The agent's
 * flash_read_id reads bytes [0..2] of an 8-byte fetch; some SPI NAND chips
 * return the manufacturer ID with a leading dummy byte, so we accept the ID
 * shifted by one position too. */
static int nand_identify(const uint8_t id[3]) {
    /* Direct: id[0]=0xC2 id[1]=0x12 */
    if (id[0] == 0xC2 && id[1] == 0x12) return 1;
    /* Shifted by 1 (dummy byte at id[0]): id[1]=0xC2 id[2]=0x12 */
    if (id[1] == 0xC2 && id[2] == 0x12) return 1;
    return 0;
}

int flash_init(flash_info_t *info) {
    /* Ensure FMC clock is enabled and not in reset */
    uint32_t crg = REG_FMC_CRG;
    crg |= FMC_CLK_ENABLE;
    crg &= ~FMC_SOFT_RESET;
    REG_FMC_CRG = crg;

    /* Verify FMC IP version */
    if (fmc_reg(FMC_VERSION) != 0x100) return -1;

    /* Set SPI timing parameters */
    fmc_reg(FMC_SPI_TIMING_CFG) = SPI_TIMING_VAL;

    /* Clear pending interrupts */
    fmc_reg(FMC_INT_CLR) = 0xFF;

    /* Read JEDEC ID (requires normal mode) */
    fmc_enter_normal();
    flash_read_id(info->jedec_id);

    if (nand_identify(info->jedec_id)) {
        /* SPI NAND path. No flash_unlock / fmc_enter_boot — NAND has no
         * memory-mapped boot mode and uses different protection (BP bits
         * via SET_FEATURE 0xA0 instead of write-status-register). */
        info->flash_type = FLASH_TYPE_NAND;
        info->size = 128u * 1024u * 1024u;     /* MX35LF1GE4AB = 128 MiB */
        info->sector_size = NAND_BLOCK_SIZE;    /* 128 KiB erase block */
        info->page_size = NAND_PAGE_SIZE;       /* 2 KiB read/program page */
        current_flash_type = FLASH_TYPE_NAND;

        /* Clear block-protection bits (BP0..BP3 + BRWD) in feature 0xA0 so
         * subsequent erase/program commands aren't rejected.  Most SPI
         * NAND chips (MX35LF, GD5F, W25N, ...) ship with all blocks
         * locked; this is the equivalent of NOR's flash_unlock. */
        nand_set_feature(NAND_FEATURE_PROTECT, 0x00);
        return 0;
    }

    /* SPI NOR path */
    fmc_enter_boot();

    info->flash_type = FLASH_TYPE_NOR;
    info->size = detect_size(info->jedec_id[2]);
    info->sector_size = 0x10000;  /* 64KB */
    info->page_size = 256;
    current_flash_type = FLASH_TYPE_NOR;

    return (info->jedec_id[0] != 0x00 && info->jedec_id[0] != 0xFF) ? 0 : -1;
}

/* Wait for OIP=0 in the SPI NAND status feature.  Returns the final status
 * byte so callers can check E_FAIL / P_FAIL.  Returns 0xFF on timeout
 * (callers will see this as "all flags set" and treat it as failure). */
static uint8_t nand_wait_oip(void) {
    for (uint32_t i = 0; i < 10000000; i++) {
        uint8_t status = nand_get_feature(NAND_FEATURE_STATUS);
        if (!(status & NAND_STATUS_OIP)) return status;
        proto_drain_fifo();  /* keep host-side UART happy during long ops */
    }
    return 0xFF;
}

/* Erase one 128 KiB block.  `row` is the page index of any page within the
 * block (block boundary = row & ~63).  Returns 0 on success, -1 on E_FAIL. */
static int nand_erase_block(uint32_t row) {
    nand_write_enable();

    fmc_reg(FMC_INT_CLR) = 0xFF;
    fmc_reg(FMC_CMD) = SPI_CMD_NAND_BLOCK_ERASE;
    fmc_reg(FMC_ADDRL) = row;
    fmc_reg(FMC_OP_CFG) = OP_CFG_OEN_EN | OP_CFG_CS(0) | OP_CFG_ADDR_NUM(3);
    fmc_reg(FMC_OP) = FMC_OP_CMD1_EN | FMC_OP_ADDR_EN | FMC_OP_REG_OP_START;
    fmc_wait_ready();

    uint8_t status = nand_wait_oip();
    return (status & NAND_STATUS_E_FAIL) ? -1 : 0;
}

/* Program up to one full 2 KiB page.  `row` is the page index, `column` is
 * the byte offset within the page (typically 0 for full-page writes).
 * Loads data into the chip's cache via PROGRAM_LOAD (0x02 first chunk,
 * 0x84 random-load for subsequent chunks so we don't reset the cache),
 * then commits with PROGRAM_EXECUTE.  Returns 0 on success, -1 on
 * P_FAIL.  On-chip ECC computes spare-area bytes transparently. */
static int nand_program_page(uint32_t row, uint32_t column,
                             const uint8_t *data, uint32_t len) {
    nand_write_enable();

    /* Chunk PROGRAM_LOAD into the FMC's 256-byte I/O buffer.  The first
     * chunk uses 0x02 (resets cache to all 0xFF before loading); subsequent
     * chunks use 0x84 (random load — preserves earlier chunks). */
    volatile uint8_t *iobuf = (volatile uint8_t *)(FLASH_MEM);
    uint32_t off = 0;
    int first_chunk = 1;
    while (off < len) {
        uint32_t chunk = (len - off > 256) ? 256 : (len - off);
        for (uint32_t i = 0; i < chunk; i++)
            iobuf[i] = data[off + i];

        fmc_reg(FMC_INT_CLR) = 0xFF;
        fmc_reg(FMC_CMD) = first_chunk ? SPI_CMD_NAND_PROGRAM_LOAD
                                       : SPI_CMD_NAND_PROGRAM_RAND;
        fmc_reg(FMC_ADDRL) = column + off;
        fmc_reg(FMC_OP_CFG) = OP_CFG_OEN_EN | OP_CFG_CS(0) | OP_CFG_ADDR_NUM(2);
        fmc_reg(FMC_DATA_NUM) = chunk;
        fmc_reg(FMC_OP) = FMC_OP_CMD1_EN | FMC_OP_ADDR_EN | FMC_OP_WRITE_DATA | FMC_OP_REG_OP_START;
        fmc_wait_ready();

        first_chunk = 0;
        off += chunk;
    }

    /* Commit cache to NAND array. */
    fmc_reg(FMC_INT_CLR) = 0xFF;
    fmc_reg(FMC_CMD) = SPI_CMD_NAND_PROGRAM_EXEC;
    fmc_reg(FMC_ADDRL) = row;
    fmc_reg(FMC_OP_CFG) = OP_CFG_OEN_EN | OP_CFG_CS(0) | OP_CFG_ADDR_NUM(3);
    fmc_reg(FMC_OP) = FMC_OP_CMD1_EN | FMC_OP_ADDR_EN | FMC_OP_REG_OP_START;
    fmc_wait_ready();

    uint8_t status = nand_wait_oip();
    return (status & NAND_STATUS_P_FAIL) ? -1 : 0;
}

/* Read up to NAND_PAGE_SIZE bytes from a NAND page (data area only).
 * row = page index (0 .. flash_size/page_size - 1)
 * column = byte offset within the 2 KiB data area (0 .. NAND_PAGE_SIZE-1)
 * On-chip ECC is left at its power-on default (enabled on MX35LF*) so the
 * returned bytes are ECC-corrected.
 *
 * Implementation: PAGE_READ (loads page → chip cache) → wait OIP →
 * chunked READ_FROM_CACHE.  The FMC captures the chip's 8-cycle dummy as
 * byte 0 of its I/O buffer (always 0x00 since the chip drives the dummy
 * line low), so real chip data starts at iobuf[1].  We compensate by
 * requesting `chunk + 1` bytes per fetch and copying iobuf[1..chunk]. */
static void nand_read(uint32_t row, uint32_t column,
                      uint8_t *buf, uint32_t len) {
    /* 1) PAGE_READ: load page from array into chip cache. */
    fmc_reg(FMC_INT_CLR) = 0xFF;
    fmc_reg(FMC_CMD) = SPI_CMD_NAND_PAGE_READ;
    fmc_reg(FMC_ADDRL) = row;
    fmc_reg(FMC_OP_CFG) = OP_CFG_OEN_EN | OP_CFG_CS(0) | OP_CFG_ADDR_NUM(3);
    fmc_reg(FMC_OP) = FMC_OP_CMD1_EN | FMC_OP_ADDR_EN | FMC_OP_REG_OP_START;
    fmc_wait_ready();

    /* 2) Wait for OIP=0 — chip finishes ECC correction and signals ready. */
    nand_wait_oip();

    /* 3) READ_FROM_CACHE: pull data from cache via column addressing.
     * FMC stores the post-address dummy byte as iobuf[0] (always 0x00),
     * so real data lives at iobuf[1..N].  We request (chunk + 1) bytes
     * per fetch and skip iobuf[0]; max useful chunk is 255 since iobuf
     * is 256 bytes. */
    volatile uint8_t *iobuf = (volatile uint8_t *)(FLASH_MEM);
    uint32_t off = 0;
    while (off < len) {
        uint32_t chunk = (len - off > 255) ? 255 : (len - off);
        fmc_reg(FMC_INT_CLR) = 0xFF;
        fmc_reg(FMC_CMD) = SPI_CMD_NAND_READ_CACHE;
        fmc_reg(FMC_ADDRL) = column + off;
        fmc_reg(FMC_DATA_NUM) = chunk + 1;   /* +1 to capture the dummy byte */
        fmc_reg(FMC_OP_CFG) = OP_CFG_OEN_EN | OP_CFG_CS(0)
                            | OP_CFG_ADDR_NUM(2)
                            | OP_CFG_DUMMY_NUM(0);   /* dummy is implicit in iobuf[0] */
        fmc_reg(FMC_OP) = FMC_OP_CMD1_EN | FMC_OP_ADDR_EN | FMC_OP_READ_DATA | FMC_OP_REG_OP_START;
        fmc_wait_ready();
        for (uint32_t i = 0; i < chunk; i++)
            buf[off + i] = iobuf[i + 1];   /* skip iobuf[0] = dummy */
        off += chunk;
    }
}

void flash_read(uint32_t addr, uint8_t *buf, uint32_t len) {
    if (current_flash_type == FLASH_TYPE_NAND) {
        /* NAND: convert byte offset to (page, column) and read page-by-page
         * via PAGE_READ + READ_FROM_CACHE.  No mode-switching — NAND lives
         * entirely in normal mode. */
        while (len > 0) {
            uint32_t row = addr / NAND_PAGE_SIZE;
            uint32_t col = addr % NAND_PAGE_SIZE;
            uint32_t in_page = NAND_PAGE_SIZE - col;
            uint32_t chunk = (len < in_page) ? len : in_page;
            nand_read(row, col, buf, chunk);
            buf += chunk;
            addr += chunk;
            len -= chunk;
        }
        return;
    }

    /* NOR: register-based reads (normal mode) instead of memory window.
     * The boot mode memory window wraps at 1MB on some SoCs. */
    fmc_enter_normal();
    volatile uint8_t *iobuf = (volatile uint8_t *)(FLASH_MEM);

    while (len > 0) {
        uint32_t chunk = len > 256 ? 256 : len;
        fmc_reg(FMC_CMD) = 0x03;  /* SPI READ */
        fmc_reg(FMC_ADDRL) = addr;
        fmc_reg(FMC_DATA_NUM) = chunk;
        fmc_reg(FMC_OP_CFG) = OP_CFG_OEN_EN | OP_CFG_CS(0) | OP_CFG_ADDR_NUM(3);
        fmc_reg(FMC_OP) = FMC_OP_CMD1_EN | FMC_OP_ADDR_EN | FMC_OP_READ_DATA | FMC_OP_REG_OP_START;
        fmc_wait_ready();

        for (uint32_t i = 0; i < chunk; i++)
            buf[i] = iobuf[i];

        buf += chunk;
        addr += chunk;
        len -= chunk;
    }

    fmc_enter_boot();
}

uint8_t flash_unlock_debug[3];

/* Read SPI flash status register (must be in normal mode).
 * Uses READ_DATA path (reads into I/O buffer) instead of READ_STATUS
 * path (FMC_STATUS register) for more reliable results. */
uint8_t flash_read_status(void) {
    fmc_reg(FMC_CMD) = SPI_CMD_READ_STATUS;
    fmc_reg(FMC_OP_CFG) = OP_CFG_OEN_EN | OP_CFG_CS(0);
    fmc_reg(FMC_DATA_NUM) = 1;
    fmc_reg(FMC_OP) = FMC_OP_CMD1_EN | FMC_OP_READ_DATA | FMC_OP_REG_OP_START;
    fmc_wait_ready();
    return (uint8_t)(*(volatile uint32_t *)(FLASH_MEM) & 0xFF);
}

int flash_erase_sector(uint32_t addr) {
    if (current_flash_type == FLASH_TYPE_NAND) {
        /* `addr` is the byte offset of any page within the block.
         * Convert to a row (page index); chip rounds down to block. */
        return nand_erase_block(addr / NAND_PAGE_SIZE);
    }
    fmc_enter_normal();

    /* Disable hardware write protect — must be done every time because
     * fmc_enter_boot (soft reset) restores the GLOBAL_CFG register. */
    fmc_reg(FMC_GLOBAL_CFG) = fmc_reg(FMC_GLOBAL_CFG) & ~(1 << 2);

    flash_unlock();
    spi_write_enable();

    fmc_reg(FMC_CMD) = SPI_CMD_SECTOR_ERASE;
    fmc_reg(FMC_ADDRL) = addr;
    fmc_reg(FMC_OP_CFG) = OP_CFG_OEN_EN | OP_CFG_CS(0) | OP_CFG_ADDR_NUM(3);
    fmc_reg(FMC_OP) = FMC_OP_CMD1_EN | FMC_OP_ADDR_EN | FMC_OP_REG_OP_START;
    fmc_wait_ready();

    /* Erase takes up to 150ms for 64KB sector — poll WIP */
    spi_wait_wip();

    /* Soft-reset FMC to return to boot mode for memory-mapped reads */
    fmc_enter_boot();

    /* Re-verify status — soft reset may re-load flash status from hardware */
    return 0;
}

int flash_write_page(uint32_t addr, const uint8_t *data, uint32_t len) {
    if (current_flash_type == FLASH_TYPE_NAND) {
        /* For NAND, "page" is 2 KiB and writes can span the full page in
         * one command.  Caller may still pass smaller chunks (256 B),
         * which we accept — but each call is one PROGRAM cycle, so
         * efficiency is best at full-page boundaries. */
        uint32_t row = addr / NAND_PAGE_SIZE;
        uint32_t column = addr % NAND_PAGE_SIZE;
        if (len > NAND_PAGE_SIZE - column)
            len = NAND_PAGE_SIZE - column;
        return nand_program_page(row, column, data, len);
    }

    if (len > 256) len = 256;

    fmc_enter_normal();
    fmc_reg(FMC_GLOBAL_CFG) = fmc_reg(FMC_GLOBAL_CFG) & ~(1 << 2);
    flash_unlock();
    spi_write_enable();

    /* Copy data to FMC I/O buffer (memory window in normal mode) */
    volatile uint8_t *fmc_buf = (volatile uint8_t *)(FLASH_MEM);
    for (uint32_t i = 0; i < len; i++)
        fmc_buf[i] = data[i];

    fmc_reg(FMC_CMD) = SPI_CMD_PAGE_PROGRAM;
    fmc_reg(FMC_ADDRL) = addr;
    fmc_reg(FMC_DATA_NUM) = len;
    fmc_reg(FMC_OP_CFG) = OP_CFG_OEN_EN | OP_CFG_CS(0) | OP_CFG_ADDR_NUM(3);
    fmc_reg(FMC_OP) = FMC_OP_CMD1_EN | FMC_OP_ADDR_EN | FMC_OP_WRITE_DATA | FMC_OP_REG_OP_START;
    fmc_wait_ready();

    spi_wait_wip();
    fmc_enter_boot();
    return 0;
}

int flash_read_oob(uint32_t block, uint8_t *buf, uint32_t len) {
    if (current_flash_type != FLASH_TYPE_NAND) return -1;
    if (len > 64) len = 64;

    /* OOB lives at column NAND_PAGE_SIZE..NAND_PAGE_SIZE+63 of page 0 of
     * the block.  Issue PAGE_READ for the first page of the block, wait
     * OIP, then READ_FROM_CACHE at column = NAND_PAGE_SIZE.  Same
     * iobuf[1] dummy-byte skip as nand_read. */
    uint32_t row = block * (NAND_BLOCK_SIZE / NAND_PAGE_SIZE);  /* = block * 64 */

    fmc_reg(FMC_INT_CLR) = 0xFF;
    fmc_reg(FMC_CMD) = SPI_CMD_NAND_PAGE_READ;
    fmc_reg(FMC_ADDRL) = row;
    fmc_reg(FMC_OP_CFG) = OP_CFG_OEN_EN | OP_CFG_CS(0) | OP_CFG_ADDR_NUM(3);
    fmc_reg(FMC_OP) = FMC_OP_CMD1_EN | FMC_OP_ADDR_EN | FMC_OP_REG_OP_START;
    fmc_wait_ready();
    nand_wait_oip();

    volatile uint8_t *iobuf = (volatile uint8_t *)(FLASH_MEM);
    fmc_reg(FMC_INT_CLR) = 0xFF;
    fmc_reg(FMC_CMD) = SPI_CMD_NAND_READ_CACHE;
    fmc_reg(FMC_ADDRL) = NAND_PAGE_SIZE;          /* column = start of OOB */
    fmc_reg(FMC_DATA_NUM) = len + 1;              /* +1 for the dummy byte */
    fmc_reg(FMC_OP_CFG) = OP_CFG_OEN_EN | OP_CFG_CS(0)
                        | OP_CFG_ADDR_NUM(2)
                        | OP_CFG_DUMMY_NUM(0);
    fmc_reg(FMC_OP) = FMC_OP_CMD1_EN | FMC_OP_ADDR_EN | FMC_OP_READ_DATA | FMC_OP_REG_OP_START;
    fmc_wait_ready();
    for (uint32_t i = 0; i < len; i++)
        buf[i] = iobuf[i + 1];   /* skip iobuf[0] = dummy */
    return 0;
}

uint32_t flash_crc32(uint32_t addr, uint32_t len) {
    /* Use register-based reads to compute CRC32 over flash region.
     * Boot mode memory window wraps at 1MB on some SoCs. */
    fmc_enter_normal();
    volatile uint8_t *iobuf = (volatile uint8_t *)(FLASH_MEM);
    uint32_t c = 0;

    while (len > 0) {
        uint32_t chunk = len > 256 ? 256 : len;
        fmc_reg(FMC_CMD) = 0x03;  /* SPI READ */
        fmc_reg(FMC_ADDRL) = addr;
        fmc_reg(FMC_DATA_NUM) = chunk;
        fmc_reg(FMC_OP_CFG) = OP_CFG_OEN_EN | OP_CFG_CS(0) | OP_CFG_ADDR_NUM(3);
        fmc_reg(FMC_OP) = FMC_OP_CMD1_EN | FMC_OP_ADDR_EN | FMC_OP_READ_DATA | FMC_OP_REG_OP_START;
        fmc_wait_ready();

        /* CRC the I/O buffer contents in place */
        uint8_t tmp[256];
        for (uint32_t i = 0; i < chunk; i++)
            tmp[i] = iobuf[i];
        c = crc32(c, tmp, chunk);

        addr += chunk;
        len -= chunk;
    }

    fmc_enter_boot();
    return c;
}
