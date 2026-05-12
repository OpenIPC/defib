/*
 * HiSilicon HISFC350 SPI flash driver — NOR-only.
 *
 * Used by V1-era HiSilicon SoCs (hi3520dv200, hi3518ev100, hi3516cv100,
 * hi3535, hi3516a). Register layout differs entirely from the FMC100
 * driver in spi_flash.c — FMC100 packs cmd+addr+data into FMC_OP/CFG at
 * offsets 0x24-0x3C, HISFC350 has separate CMD_INS (0x308), CMD_ADDR
 * (0x30C) and a 64-byte CMD_DATABUF (0x400-0x43C).
 *
 * Reads use the AHB memory-mapped window (FLASH_MEM) which the bootrom
 * leaves enabled. Erase / program / RDID use the controller's CMD_*
 * register interface. For 32 MiB+ chips we issue EN4B and flip the
 * controller's GLOBAL_CONFIG.ADDR_MODE_4B so memory-mapped reads cover
 * the full chip.
 *
 * Reference: vendor U-Boot drivers/mtd/spi/hisfc350/.
 */

#include "spi_flash.h"
#include "protocol.h"

/* HISFC350 register offsets */
#define HISFC350_GLOBAL_CONFIG               0x100
#define HISFC350_GLOBAL_CONFIG_ADDR_MODE_4B  (1 << 2)
#define HISFC350_TIMING                      0x110
#define HISFC350_TIMING_VAL                  ((6 << 12) | (6 << 8) | 0xF)
#define HISFC350_INT_CLEAR                   0x12C
#define HISFC350_VERSION                     0x1F8

#define HISFC350_BUS_CONFIG1                    0x200
#define HISFC350_BUS_CONFIG1_READ_EN            (1u << 31)
#define HISFC350_BUS_CONFIG1_READ_INS(_n)       (((_n) & 0xFF) << 8)
#define HISFC350_BUS_CONFIG1_READ_DUMMY_CNT(_n) (((_n) & 0x7) << 3)
#define HISFC350_BUS_CONFIG1_READ_IF_TYPE(_n)   ((_n) & 0x7)

#define HISFC350_CMD_CONFIG                  0x300
#define HISFC350_CMD_CONFIG_DATA_CNT(_n)     ((((_n) - 1) & 0x3F) << 9)
#define HISFC350_CMD_CONFIG_RW_READ          (1 << 8)
#define HISFC350_CMD_CONFIG_DATA_EN          (1 << 7)
#define HISFC350_CMD_CONFIG_ADDR_EN          (1 << 3)
#define HISFC350_CMD_CONFIG_SEL_CS(_cs)      (((_cs) & 0x01) << 1)
#define HISFC350_CMD_CONFIG_START            (1 << 0)
#define HISFC350_CMD_INS                     0x308
#define HISFC350_CMD_ADDR                    0x30C
#define HISFC350_CMD_DATABUF0                0x400

/* HISFC350 cmd-mode max payload per single CMD_CONFIG_START — DATA_CNT
 * field is 6 bits (1..64). Page programs are split into 64-byte chunks. */
#define HISFC_CMD_BUF_MAX  64

/* SPI NOR commands */
#define SPI_CMD_RDID        0x9F
#define SPI_CMD_RDSR        0x05
#define SPI_CMD_WRSR        0x01
#define SPI_CMD_WREN        0x06
#define SPI_CMD_PP          0x02
#define SPI_CMD_SE          0xD8
#define SPI_CMD_EN4B        0xB7
/* Macronix Extended Address Register (EAR): MX25L256-class chips keep
 * 3-byte address mode but use EAR bit 0 as the 25th address bit. WREAR
 * (0xC5) writes EAR. Vendor U-Boot's `SPI_BRWR=0x17` defines for this
 * chip are Spansion convention (BAR), not Macronix EAR — they're dead
 * code in the vendor MX25L25635E driver. EN4B (0xB7) was tried and the
 * controller's ADDR_MODE_4B latches but reads past 16 MiB still wrap,
 * so we use the EAR-banking path instead. */
#define SPI_CMD_WREAR       0xC5       /* Write Extended Address Register */
#define SPI_CMD_RDEAR       0xC8       /* Read Extended Address Register  */
#define BAR_HIGH            0x01       /* EAR bit 0 set: upper 16 MiB */
#define BAR_LOW             0x00       /* EAR cleared: lower 16 MiB   */

#define SPI_STATUS_WIP      (1 << 0)
#define SPI_STATUS_BP_MASK  0x7C   /* BP0..BP4: bits 2-6 */

uint8_t flash_unlock_debug[3];

/* Active chip-select for the populated SPI flash. HISFC350 supports two
 * chip-selects; vendor boards split usage (e.g. hi3520dv200 reference
 * board puts the NOR on CS1 and leaves CS0 empty). flash_init probes
 * both and latches whichever returns a valid JEDEC ID. */
static int active_cs = 0;

/* Whether this chip needs bank-switching for accesses past 16 MiB.
 * Set in flash_init when the chip is larger than 16 MiB. */
static int needs_bank_switch = 0;
/* Currently-selected bank (0 = low 16 MiB, 1 = high 16 MiB). 0xff =
 * unknown, force a switch on first access. */
static uint8_t current_bank = 0xff;

static uint32_t detect_size(uint8_t id2) {
    if (id2 >= 0x14 && id2 <= 0x1A) return 1u << id2;
    return 0x1000000;  /* default 16 MiB */
}

/* Wait for HISFC350 cmd-mode operation to finish. */
static void hisfc_wait_cmd(void) {
    volatile uint32_t timeout = 0x1000000;
    while ((fmc_reg(HISFC350_CMD_CONFIG) & HISFC350_CMD_CONFIG_START) && timeout)
        timeout--;
}

void flash_read_id(uint8_t id[3]) {
    fmc_reg(HISFC350_CMD_INS) = SPI_CMD_RDID;
    fmc_reg(HISFC350_CMD_CONFIG) =
        HISFC350_CMD_CONFIG_SEL_CS(active_cs) |
        HISFC350_CMD_CONFIG_RW_READ |
        HISFC350_CMD_CONFIG_DATA_EN |
        HISFC350_CMD_CONFIG_DATA_CNT(8) |
        HISFC350_CMD_CONFIG_START;
    hisfc_wait_cmd();

    uint32_t w = fmc_reg(HISFC350_CMD_DATABUF0);
    id[0] = (w >> 0)  & 0xFF;
    id[1] = (w >> 8)  & 0xFF;
    id[2] = (w >> 16) & 0xFF;
}

uint8_t flash_read_status(void) {
    fmc_reg(HISFC350_CMD_INS) = SPI_CMD_RDSR;
    fmc_reg(HISFC350_CMD_CONFIG) =
        HISFC350_CMD_CONFIG_SEL_CS(active_cs) |
        HISFC350_CMD_CONFIG_RW_READ |
        HISFC350_CMD_CONFIG_DATA_EN |
        HISFC350_CMD_CONFIG_DATA_CNT(1) |
        HISFC350_CMD_CONFIG_START;
    hisfc_wait_cmd();
    return (uint8_t)(fmc_reg(HISFC350_CMD_DATABUF0) & 0xFF);
}

static void spi_wait_wip(void) {
    /* WIP can stay set for ~150 ms during a sector erase. We poll WIP
     * via cmd-mode RDSR; each poll is a ~few-µs SPI transaction. The
     * outer loop bound is generous to absorb worst-case erase. */
    for (int i = 0; i < 10000000; i++) {
        if (!(flash_read_status() & SPI_STATUS_WIP)) return;
        proto_drain_fifo();
    }
}

static void spi_write_enable(void) {
    fmc_reg(HISFC350_CMD_INS) = SPI_CMD_WREN;
    fmc_reg(HISFC350_CMD_CONFIG) =
        HISFC350_CMD_CONFIG_SEL_CS(active_cs) |
        HISFC350_CMD_CONFIG_START;
    hisfc_wait_cmd();
}

/* Clear BP0..BP4 in SR1 if any are set. HiSilicon SPL/bootroms often
 * leave the chip with all sectors locked — every erase/program is a
 * silent no-op until BP is cleared. */
static void flash_unlock(void) {
    uint8_t sr_before = flash_read_status();
    if (!(sr_before & SPI_STATUS_BP_MASK)) {
        flash_unlock_debug[0] = sr_before;
        flash_unlock_debug[1] = sr_before;
        flash_unlock_debug[2] = sr_before;
        return;
    }

    spi_write_enable();
    uint8_t sr_wel = flash_read_status();

    fmc_reg(HISFC350_CMD_DATABUF0) = sr_before & ~SPI_STATUS_BP_MASK;
    fmc_reg(HISFC350_CMD_INS) = SPI_CMD_WRSR;
    fmc_reg(HISFC350_CMD_CONFIG) =
        HISFC350_CMD_CONFIG_SEL_CS(active_cs) |
        HISFC350_CMD_CONFIG_DATA_EN |
        HISFC350_CMD_CONFIG_DATA_CNT(1) |
        HISFC350_CMD_CONFIG_START;
    hisfc_wait_cmd();
    spi_wait_wip();

    flash_unlock_debug[0] = sr_before;
    flash_unlock_debug[1] = sr_wel;
    flash_unlock_debug[2] = flash_read_status();
}

/* Select bank 0 (low 16 MiB) or 1 (high 16 MiB) via Macronix BRWR.
 * Chip stays in 3-byte address mode; the BAR register provides the
 * 25th address bit so 3-byte addresses cover either half. Idempotent —
 * skips the SPI transaction when already in the requested bank. */
static void flash_select_bank(uint8_t bank) {
    if (!needs_bank_switch || bank == current_bank) return;

    spi_write_enable();   /* WREN before any register write */

    fmc_reg(HISFC350_CMD_DATABUF0) = (bank ? BAR_HIGH : BAR_LOW);
    fmc_reg(HISFC350_CMD_INS) = SPI_CMD_WREAR;
    fmc_reg(HISFC350_CMD_CONFIG) =
        HISFC350_CMD_CONFIG_SEL_CS(active_cs) |
        HISFC350_CMD_CONFIG_DATA_EN |
        HISFC350_CMD_CONFIG_DATA_CNT(1) |
        HISFC350_CMD_CONFIG_START;
    hisfc_wait_cmd();
    spi_wait_wip();

    current_bank = bank;
}

int flash_init(flash_info_t *info) {
    /* Sanity: refuse to run on the wrong controller. The HISFC350
     * VERSION register reads as 0x350 on real silicon. */
    if (fmc_reg(HISFC350_VERSION) != 0x350) return -1;

    /* Set timing to vendor defaults — bootrom leaves something usable
     * but vendor U-Boot reprograms this on every probe. */
    fmc_reg(HISFC350_TIMING) = HISFC350_TIMING_VAL;

    /* Probe both chip-selects. Vendor U-Boot scans from CS_MAX-1 down
     * to CS0 and binds the first that returns a valid JEDEC. Vendor
     * boards split CS1 (e.g. hi3520dv200) and CS0 (most others). */
    for (int cs = 1; cs >= 0; cs--) {
        active_cs = cs;
        flash_read_id(info->jedec_id);
        uint8_t a = info->jedec_id[0], b = info->jedec_id[1], c = info->jedec_id[2];
        if ((a | b | c) != 0 && (a & b & c) != 0xFF)
            goto found;
    }
    return -1;

found:
    info->size        = detect_size(info->jedec_id[2]);
    info->sector_size = 0x10000;   /* 64 KiB NOR sector */
    info->page_size   = 256;
    info->flash_type  = FLASH_TYPE_NOR;

    /* For >16 MiB chips, use Macronix BRWR (Bank Address Register) to
     * swap the high 16 MiB into the chip's 24-bit address window. The
     * controller stays in 3-byte mode so the existing memory-mapped
     * read window keeps working — flash_select_bank rewrites the chip's
     * BAR before any access that crosses the 16 MiB boundary. EN4B
     * (true 4-byte mode) was tried but reads past 16 MiB returned a
     * fixed repeating pattern; BRWR is the path the vendor U-Boot
     * driver hisfc350_spi_mx25l25635e.c also uses for this part. */
    if (info->size > 0x1000000) {
        needs_bank_switch = 1;
        flash_select_bank(BAR_LOW);   /* start at low bank */
    }

    return 0;
}

/* Iterate over a flash range in chunks that stay within a single bank,
 * switching the chip's BAR as needed. Calls `step(bank_off, ptr, n)`
 * for each chunk where ptr is the AHB-mapped address inside the
 * currently-selected bank. */
static void flash_walk_banked(uint32_t addr, uint32_t len,
                              void (*step)(uint32_t off,
                                           const volatile uint8_t *ptr,
                                           uint32_t n,
                                           void *ctx),
                              void *ctx) {
    while (len > 0) {
        uint8_t bank = (addr >= 0x1000000) ? 1 : 0;
        flash_select_bank(bank);
        uint32_t off_in_bank = addr & 0xFFFFFF;
        uint32_t avail       = 0x1000000 - off_in_bank;
        uint32_t chunk       = (len < avail) ? len : avail;
        const volatile uint8_t *p =
            (const volatile uint8_t *)(FLASH_MEM + off_in_bank);
        step(addr, p, chunk, ctx);
        addr += chunk;
        len  -= chunk;
    }
}

struct read_ctx { uint8_t *buf; uint32_t base; };
static void read_step(uint32_t off, const volatile uint8_t *p,
                      uint32_t n, void *vctx) {
    struct read_ctx *c = vctx;
    uint8_t *dst = c->buf + (off - c->base);
    for (uint32_t i = 0; i < n; i++) dst[i] = p[i];
}

void flash_read(uint32_t addr, uint8_t *buf, uint32_t len) {
    if (!needs_bank_switch) {
        const volatile uint8_t *src = (const volatile uint8_t *)(FLASH_MEM + addr);
        for (uint32_t i = 0; i < len; i++) buf[i] = src[i];
        return;
    }
    struct read_ctx c = { buf, addr };
    flash_walk_banked(addr, len, read_step, &c);
}

struct crc_ctx { uint32_t crc; };
static void crc_step(uint32_t off, const volatile uint8_t *p,
                     uint32_t n, void *vctx) {
    (void)off;
    struct crc_ctx *c = vctx;
    c->crc = crc32(c->crc, (const uint8_t *)p, n);
}

uint32_t flash_crc32(uint32_t addr, uint32_t len) {
    if (!needs_bank_switch) {
        return crc32(0, (const uint8_t *)(FLASH_MEM + addr), len);
    }
    struct crc_ctx c = { 0 };
    flash_walk_banked(addr, len, crc_step, &c);
    return c.crc;
}

/* Verify a just-erased range really reads back as 0xFF. Catches the
 * silent-no-op failure mode where BP bits or WPS made the controller
 * accept an erase command without the chip actually clearing cells.
 * Routes through flash_read so the upper-bank verify works too. */
static int flash_verify_erased(uint32_t addr, uint32_t len) {
    uint8_t buf[16];
    uint32_t head = len < sizeof(buf) ? len : sizeof(buf);
    flash_read(addr, buf, head);
    for (uint32_t i = 0; i < head; i++) if (buf[i] != 0xFF) return -1;
    if (len > 32) {
        flash_read(addr + len - sizeof(buf), buf, sizeof(buf));
        for (uint32_t i = 0; i < sizeof(buf); i++) if (buf[i] != 0xFF) return -1;
    }
    return 0;
}

int flash_erase_sector(uint32_t addr) {
    /* Bank-switch BEFORE the erase so the chip's BAR supplies bit 24
     * of the address (CMD_ADDR carries only the lower 24 bits). */
    if (needs_bank_switch)
        flash_select_bank(addr >= 0x1000000 ? 1 : 0);

    flash_unlock();
    spi_write_enable();

    fmc_reg(HISFC350_CMD_INS)  = SPI_CMD_SE;
    fmc_reg(HISFC350_CMD_ADDR) = addr & 0xFFFFFF;
    fmc_reg(HISFC350_CMD_CONFIG) =
        HISFC350_CMD_CONFIG_SEL_CS(active_cs) |
        HISFC350_CMD_CONFIG_ADDR_EN |
        HISFC350_CMD_CONFIG_START;
    hisfc_wait_cmd();
    spi_wait_wip();

    if (flash_verify_erased(addr, 0x10000) != 0) return -1;
    return 0;
}

int flash_write_page(uint32_t addr, const uint8_t *data, uint32_t len) {
    if (len > 256) len = 256;

    if (needs_bank_switch)
        flash_select_bank(addr >= 0x1000000 ? 1 : 0);

    flash_unlock();

    /* HISFC350 cmd-mode caps DATA_CNT at 64 bytes, so a 256-byte page
     * splits into 4 PP cycles. Each chunk is its own WREN + PP. The
     * chip handles the address increment within the page. */
    uint32_t offset = 0;
    while (offset < len) {
        uint32_t chunk = len - offset;
        if (chunk > HISFC_CMD_BUF_MAX) chunk = HISFC_CMD_BUF_MAX;

        spi_write_enable();

        for (uint32_t i = 0; i < chunk; i += 4) {
            uint32_t w = 0;
            for (uint32_t j = 0; j < 4 && (i + j) < chunk; j++)
                w |= ((uint32_t)data[offset + i + j]) << (j * 8);
            fmc_reg(HISFC350_CMD_DATABUF0 + i) = w;
        }

        fmc_reg(HISFC350_CMD_INS)  = SPI_CMD_PP;
        fmc_reg(HISFC350_CMD_ADDR) = (addr + offset) & 0xFFFFFF;
        fmc_reg(HISFC350_CMD_CONFIG) =
            HISFC350_CMD_CONFIG_SEL_CS(active_cs) |
            HISFC350_CMD_CONFIG_DATA_EN |
            HISFC350_CMD_CONFIG_DATA_CNT(chunk) |
            HISFC350_CMD_CONFIG_ADDR_EN |
            HISFC350_CMD_CONFIG_START;
        hisfc_wait_cmd();
        spi_wait_wip();

        offset += chunk;
    }
    return 0;
}

int flash_read_oob(uint32_t block, uint8_t *buf, uint32_t len) {
    (void)block; (void)buf; (void)len;
    return -1;  /* NOR has no OOB */
}

int flash_program_oob(uint32_t block, const uint8_t *buf, uint32_t len) {
    (void)block; (void)buf; (void)len;
    return -1;  /* NOR has no OOB */
}
