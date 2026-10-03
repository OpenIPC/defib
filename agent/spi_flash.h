/*
 * HiSilicon FMC SPI flash driver — supports SPI NOR + SPI NAND.
 *
 * NOR: register-based reads via FMC normal mode (faster than memory
 * window when window wraps at 1MB on some SoCs).
 * NAND: PAGE_READ → READ_FROM_CACHE flow, erase and program, plus the
 *       CMD_NAND study interface below (controller page engine, raw OOB).
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

/* ---- SPI NAND study interface (CMD_NAND) -------------------------------
 *
 * Page I/O at the level the Linux hifmc100 / xmedia_fmc100 drivers work
 * at, so their behaviour can be reproduced or varied one knob at a time:
 *
 *   NAND_XFER_REG  PAGE_READ/READ_FROM_CACHE or PROGRAM_LOAD/EXECUTE
 *                  through FMC register ops, page + full OOB, no
 *                  controller ECC.  With the chip's on-die ECC off this
 *                  is the raw array content.
 *   NAND_XFER_DMA  the controller's own page engine (FMC_OP_CTRL + DMA),
 *                  as the kernel drives it.  ECC type, page and block
 *                  size come from the FMC_CFG value passed in.
 */
#define NAND_XFER_REG   0
#define NAND_XFER_DMA   1

typedef struct {
    uint32_t fmc_cfg;   /* written to FMC_CFG before the op; 0 = leave as is */
    uint8_t  mode;      /* NAND_XFER_REG or NAND_XFER_DMA */
    uint8_t  opcode;    /* DMA: SPI read/program opcode (0x03, 0x6B, 0x02, 0x32 ...) */
    uint8_t  iftype;    /* DMA: FMC MEM_IF_TYPE (0 std, 1 dual, 2 dio, 3 quad, 4 qio) */
    uint8_t  dummy;     /* DMA read: dummy bytes after the column address */
} nand_xfer_t;

/* What the hardware said about one page operation. */
typedef struct {
    uint32_t ecc_err;   /* FMC ECC_ERR_NUM0_BUF0: one byte per ECC step,
                         * 0xff = uncorrectable (DMA reads only) */
    uint8_t  ondie;     /* chip status, feature 0xC0, after the op */
    uint8_t  fmc_int;   /* FMC_INT after the op */
    uint8_t  flags;     /* NAND_REC_* */
    uint8_t  rsvd;
} nand_rec_t;

#define NAND_REC_TIMEOUT    (1 << 0)    /* controller or chip never finished */
#define NAND_REC_FAIL       (1 << 1)    /* chip reported P_FAIL / E_FAIL */

/* Geometry of the identified chip; page_size 0 if it is not a SPI NAND. */
typedef struct {
    uint16_t page_size;
    uint16_t oob_size;          /* physical spare area */
    uint16_t pages_per_block;
    uint16_t blocks;
} nand_geom_t;

void    nand_get_geometry(nand_geom_t *geom);
uint8_t nand_feature_get(uint8_t addr);
void    nand_feature_set(uint8_t addr, uint8_t val);
int     nand_page_read(uint32_t page, const nand_xfer_t *x, uint8_t *dst,
                       nand_rec_t *rec);
int     nand_page_program(uint32_t page, const nand_xfer_t *x,
                          const uint8_t *src, nand_rec_t *rec);
int     nand_block_erase(uint32_t page, nand_rec_t *rec);

#endif /* SPI_FLASH_H */
