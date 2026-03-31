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

/* CRG register for FMC clock (hi3516ev300) */
#define CRG_BASE            0x12010000
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

static void fmc_wait_ready(void) {
    volatile uint32_t timeout = 400000;
    while ((fmc_reg(FMC_OP) & FMC_OP_REG_OP_START) && timeout > 0)
        timeout--;
}

static void spi_wait_wip(void) {
    for (int i = 0; i < 10000000; i++) {
        fmc_reg(FMC_CMD) = SPI_CMD_READ_STATUS;
        fmc_reg(FMC_OP_CFG) = OP_CFG_OEN_EN | OP_CFG_CS(0);
        fmc_reg(FMC_OP) = FMC_OP_CMD1_EN | FMC_OP_READ_STATUS | FMC_OP_REG_OP_START;
        fmc_wait_ready();
        if (!(fmc_reg(FMC_STATUS) & SPI_STATUS_WIP)) return;
    }
}

static void spi_write_enable(void) {
    fmc_reg(FMC_CMD) = SPI_CMD_WRITE_ENABLE;
    fmc_reg(FMC_OP_CFG) = OP_CFG_OEN_EN | OP_CFG_CS(0);
    fmc_reg(FMC_OP) = FMC_OP_CMD1_EN | FMC_OP_REG_OP_START;
    fmc_wait_ready();
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

    /* Switch to normal mode.
     * Keep SPI NOR selected. Preserve page/ECC config from bootrom.
     * FMC_CFG typical bootrom value: 0x1820. We only set bit 0 (normal). */
    fmc_reg(FMC_CFG) = 0x1821;  /* 0x1820 (bootrom default) | OP_MODE_NORMAL */

    /* Set SPI timing parameters */
    fmc_reg(FMC_SPI_TIMING_CFG) = SPI_TIMING_VAL;

    /* Clear pending interrupts */
    fmc_reg(FMC_INT_CLR) = 0xFF;

    /* Read JEDEC ID */
    flash_read_id(info->jedec_id);

    info->size = detect_size(info->jedec_id[2]);
    info->sector_size = 0x10000;  /* 64KB */
    info->page_size = 256;

    return (info->jedec_id[0] != 0x00 && info->jedec_id[0] != 0xFF) ? 0 : -1;
}

void flash_read(uint32_t addr, uint8_t *buf, uint32_t len) {
    const uint8_t *flash = (const uint8_t *)FLASH_MEM;
    for (uint32_t i = 0; i < len; i++)
        buf[i] = flash[addr + i];
}

int flash_erase_sector(uint32_t addr) {
    spi_write_enable();

    fmc_reg(FMC_CMD) = SPI_CMD_SECTOR_ERASE;
    fmc_reg(FMC_ADDRL) = addr;
    fmc_reg(FMC_OP_CFG) = OP_CFG_OEN_EN | OP_CFG_CS(0) | OP_CFG_ADDR_NUM(3);
    fmc_reg(FMC_OP) = FMC_OP_CMD1_EN | FMC_OP_ADDR_EN | FMC_OP_REG_OP_START;
    fmc_wait_ready();

    spi_wait_wip();
    return 0;
}

int flash_write_page(uint32_t addr, const uint8_t *data, uint32_t len) {
    if (len > 256) len = 256;

    spi_write_enable();

    /* Copy data to FMC I/O buffer (memory window) */
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
    return 0;
}

uint32_t flash_crc32(uint32_t addr, uint32_t len) {
    const uint8_t *flash = (const uint8_t *)FLASH_MEM;
    return crc32(0, &flash[addr], len);
}
