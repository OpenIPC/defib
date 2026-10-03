/*
 * RAM layout of the SPI NAND study buffers (CMD_NAND).  Plain #defines so
 * startup.S can include it: the DMA buffer's 1 MiB section is mapped
 * uncached there, which keeps the FMC's DMA and the CPU coherent without
 * any cache maintenance.
 *
 *   NAND_DMA_BUF   page data + OOB, written/read by FMC DMA; consecutive
 *                  pages are packed at (page_size + oob_size) stride
 *   NAND_STAT_BUF  one nand_rec_t per page of the last CMD_NAND read
 *
 * The DMA buffer ends 1 MiB below RAM_BASE + 16 MiB, the lowest agent
 * LOAD_ADDR, so the 16 KiB stack growing down from LOAD_ADDR never reaches
 * it.  The status buffer is past the agent and its bss; only a CMD_MEMBW
 * run with a large scratch size can overlap it.
 */

#ifndef NAND_LAYOUT_H
#define NAND_LAYOUT_H

#define NAND_DMA_BUF_OFF    0x00E00000  /* must be 1 MiB aligned */
#define NAND_DMA_BUF_SIZE   0x00100000
#define NAND_STAT_BUF_OFF   0x02000000
#define NAND_STAT_BUF_SIZE  0x00080000  /* 64 Ki pages x 8 B */

#define NAND_DMA_BUF        (RAM_BASE + NAND_DMA_BUF_OFF)
#define NAND_STAT_BUF       (RAM_BASE + NAND_STAT_BUF_OFF)

#endif /* NAND_LAYOUT_H */
