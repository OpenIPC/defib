/*
 * HiSilicon FMC SPI flash driver — supports SPI NOR + SPI NAND.
 *
 * NOR: register-based reads via FMC normal mode (faster than memory
 * window when window wraps at 1MB on some SoCs).
 * NAND: PAGE_READ → READ_FROM_CACHE flow with on-chip ECC enabled.
 *       Currently read-only (erase/write are NOR-only).
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

/* PERI_CRG controller base (set via -DCRG_BASE=...) */
#ifndef CRG_BASE
#define CRG_BASE    0x12010000
#endif

/* FMC register access */
#define fmc_reg(off) (*(volatile uint32_t *)(FMC_BASE + (off)))

/* Flash type */
#define FLASH_TYPE_NOR  0
#define FLASH_TYPE_NAND 1
#define FLASH_TYPE_EMMC 2  /* SD/eMMC over DesignWare MMC host */

/* Flash info */
typedef struct {
    uint8_t  jedec_id[3];   /* Manufacturer + device ID */
    uint32_t size;           /* Total flash size in bytes (data only, no OOB) */
    uint32_t sector_size;    /* Erase unit (NOR: 64KB sector, NAND: 128KB block) */
    uint32_t page_size;      /* Read/program unit (NOR: 256B, NAND: 2KB) */
    uint8_t  flash_type;     /* FLASH_TYPE_NOR or FLASH_TYPE_NAND */
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

/* Read N bytes of OOB (out-of-band / spare area) from a NAND page.
 * `block` is the block index (0 .. flash_size/sector_size - 1); the
 * function reads OOB of page 0 of that block.  `len` is capped at 64
 * (typical OOB size on small SPI NAND).  Returns 0 on success, -1 if
 * the chip is NOR (no OOB).  Used by handle_scan to read the factory
 * bad-block marker at OOB[0] of page 0 of every block. */
int flash_read_oob(uint32_t block, uint8_t *buf, uint32_t len);

/* Write N bytes of OOB to page 0 of a NAND block.  Mainly used to
 * write the bad-block marker (single 0x00 at OOB[0]).  The chip's
 * on-chip ECC computes spare-area ECC bytes; we only set OOB[0..N-1]
 * which sits in the user OOB area before the ECC region.  Returns 0
 * on success, -1 if NOR or program fails. */
int flash_program_oob(uint32_t block, const uint8_t *buf, uint32_t len);

#endif /* SPI_FLASH_H */
