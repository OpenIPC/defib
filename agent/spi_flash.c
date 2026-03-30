/*
 * HiSilicon FMC SPI NOR flash driver.
 *
 * Read: via memory-mapped window at FLASH_MEM (zero overhead).
 * Write/Erase: via FMC register interface.
 *
 * Register definitions from qemu-hisilicon/qemu/hw/misc/hisi-fmc.c
 */

#include "spi_flash.h"
#include "protocol.h"  /* for crc32() */

/* FMC register offsets */
#define FMC_CFG             0x00
#define FMC_CMD             0x24
#define FMC_ADDRL           0x2C
#define FMC_OP_CFG          0x30
#define FMC_DATA_NUM        0x38
#define FMC_OP              0x3C
#define FMC_STATUS          0xAC

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

/* SPI status */
#define SPI_STATUS_WIP  (1 << 0)  /* Write in progress */
#define SPI_STATUS_WEL  (1 << 1)  /* Write enable latch */

/* Known flash chips */
static uint32_t detect_size(uint8_t id2) {
    /* id2 encodes log2(size): 0x14=1MB, 0x15=2MB, 0x16=4MB, 0x17=8MB, 0x18=16MB, 0x19=32MB */
    if (id2 >= 0x14 && id2 <= 0x19) {
        return 1u << id2;
    }
    return 0x1000000; /* Default 16MB */
}

static void fmc_wait_ready(void) {
    /* Poll FMC_OP until operation complete */
    while (fmc_reg(FMC_OP) & FMC_OP_REG_OP_START) {}
}

static void spi_wait_wip(void) {
    /* Poll flash status register until WIP clears */
    for (int i = 0; i < 10000000; i++) {
        fmc_reg(FMC_CMD) = SPI_CMD_READ_STATUS;
        fmc_reg(FMC_OP) = FMC_OP_CMD1_EN | FMC_OP_READ_STATUS | FMC_OP_REG_OP_START;
        fmc_wait_ready();
        if (!(fmc_reg(FMC_STATUS) & SPI_STATUS_WIP)) return;
    }
}

static void spi_write_enable(void) {
    fmc_reg(FMC_CMD) = SPI_CMD_WRITE_ENABLE;
    fmc_reg(FMC_OP) = FMC_OP_CMD1_EN | FMC_OP_REG_OP_START;
    fmc_wait_ready();
}

void flash_read_id(uint8_t id[3]) {
    fmc_reg(FMC_CMD) = SPI_CMD_READ_ID;
    fmc_reg(FMC_DATA_NUM) = 3;
    fmc_reg(FMC_OP) = FMC_OP_CMD1_EN | FMC_OP_READ_DATA | FMC_OP_REG_OP_START;
    fmc_wait_ready();

    /* Read 3 bytes from FMC data buffer (at offset 0x100) */
    uint32_t data = *(volatile uint32_t *)(FMC_BASE + 0x100);
    id[0] = (data >> 0) & 0xFF;
    id[1] = (data >> 8) & 0xFF;
    id[2] = (data >> 16) & 0xFF;
}

int flash_init(flash_info_t *info) {
    flash_read_id(info->jedec_id);
    info->size = detect_size(info->jedec_id[2]);
    info->sector_size = 0x10000;  /* 64KB */
    info->page_size = 256;
    return (info->jedec_id[0] != 0x00 && info->jedec_id[0] != 0xFF) ? 0 : -1;
}

void flash_read(uint32_t addr, uint8_t *buf, uint32_t len) {
    /* Direct memory-mapped read — fastest possible */
    const uint8_t *flash = (const uint8_t *)FLASH_MEM;
    for (uint32_t i = 0; i < len; i++) {
        buf[i] = flash[addr + i];
    }
}

int flash_erase_sector(uint32_t addr) {
    spi_write_enable();

    fmc_reg(FMC_CMD) = SPI_CMD_SECTOR_ERASE;
    fmc_reg(FMC_ADDRL) = addr;
    fmc_reg(FMC_OP) = FMC_OP_CMD1_EN | FMC_OP_ADDR_EN | FMC_OP_REG_OP_START;
    fmc_wait_ready();

    spi_wait_wip();
    return 0;
}

int flash_write_page(uint32_t addr, const uint8_t *data, uint32_t len) {
    if (len > 256) len = 256;

    spi_write_enable();

    /* Copy data to FMC buffer (at offset 0x100) */
    volatile uint8_t *fmc_buf = (volatile uint8_t *)(FMC_BASE + 0x100);
    for (uint32_t i = 0; i < len; i++) {
        fmc_buf[i] = data[i];
    }

    fmc_reg(FMC_CMD) = SPI_CMD_PAGE_PROGRAM;
    fmc_reg(FMC_ADDRL) = addr;
    fmc_reg(FMC_DATA_NUM) = len;
    fmc_reg(FMC_OP) = FMC_OP_CMD1_EN | FMC_OP_ADDR_EN | FMC_OP_WRITE_DATA | FMC_OP_REG_OP_START;
    fmc_wait_ready();

    spi_wait_wip();
    return 0;
}

uint32_t flash_crc32(uint32_t addr, uint32_t len) {
    const uint8_t *flash = (const uint8_t *)FLASH_MEM;
    return crc32(0, &flash[addr], len);
}
