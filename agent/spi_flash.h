/*
 * HiSilicon FMC SPI NOR flash driver.
 *
 * Uses the FMC (Flash Memory Controller) register interface for
 * erase/write, and memory-mapped read window for fast reads.
 */

#ifndef SPI_FLASH_H
#define SPI_FLASH_H

#include <stdint.h>

/* Memory-mapped flash read base (set via -DFLASH_MEM=...) */
#ifndef FLASH_MEM
#define FLASH_MEM   0x14000000
#endif

/* FMC controller register base (set via -DFMC_BASE=...) */
#ifndef FMC_BASE
#define FMC_BASE    0x10000000
#endif

/* FMC register access */
#define fmc_reg(off) (*(volatile uint32_t *)(FMC_BASE + (off)))

/* Flash info */
typedef struct {
    uint8_t  jedec_id[3];   /* Manufacturer + device ID */
    uint32_t size;           /* Total flash size in bytes */
    uint32_t sector_size;    /* Erase sector size (typically 64KB) */
    uint32_t page_size;      /* Program page size (typically 256B) */
} flash_info_t;

/* Initialize flash controller, detect flash chip */
int flash_init(flash_info_t *info);

/* Read flash via memory-mapped window (fastest) */
void flash_read(uint32_t addr, uint8_t *buf, uint32_t len);

/* Read flash JEDEC ID */
void flash_read_id(uint8_t id[3]);

/* Erase a 64KB sector at addr (must be sector-aligned) */
int flash_erase_sector(uint32_t addr);

/* Program a page (up to 256 bytes, must be page-aligned) */
int flash_write_page(uint32_t addr, const uint8_t *data, uint32_t len);

/* Read SPI flash status register (must be in normal mode) */
uint8_t flash_read_status(void);

/* Debug: [0]=status_before, [1]=status_with_WEL, [2]=status_after unlock */
extern uint8_t flash_unlock_debug[3];

/* CRC32 of flash region (using memory-mapped read) */
uint32_t flash_crc32(uint32_t addr, uint32_t len);

#endif /* SPI_FLASH_H */
