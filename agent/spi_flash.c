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
#define FMC_ADDRL           0x2C
#define FMC_OP_CFG          0x30
#define FMC_DATA_NUM        0x38
#define FMC_OP              0x3C
#define FMC_STATUS          0xAC
#define FMC_VERSION         0xBC

/* FMC_CFG bits */
#define FMC_CFG_OP_MODE_NORMAL  (1 << 0)
#define FMC_CFG_FLASH_SEL_NOR   (0 << 1)

/* FMC_OP_CFG bits */
#define OP_CFG_OEN_EN       (1 << 13)
#define OP_CFG_CS(n)        ((n) << 11)
#define OP_CFG_ADDR_NUM(n)  ((n) << 4)  /* 3 or 4 byte address */

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

/* SPI status bits */
#define SPI_STATUS_WIP  (1 << 0)
#define SPI_STATUS_WEL  (1 << 1)

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
    fmc_enter_boot();

    info->size = detect_size(info->jedec_id[2]);
    info->sector_size = 0x10000;  /* 64KB */
    info->page_size = 256;

    return (info->jedec_id[0] != 0x00 && info->jedec_id[0] != 0xFF) ? 0 : -1;
}

void flash_read(uint32_t addr, uint8_t *buf, uint32_t len) {
    /* Use register-based reads (normal mode) instead of memory window.
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
