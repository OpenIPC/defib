/*
 * Minimal eMMC driver for the agent — Synopsys DesignWare MMC host
 * controller as instantiated on hi3516cv500-family SoCs (av300/dv300/cv500)
 * at EMMC_BASE = 0x10100000.
 *
 * Scope: READ ONLY (MVP). Just enough to bring the controller up,
 * complete the eMMC card identification sequence (CMD0/1/2/3/9/7),
 * and read one 512-byte block at a time via CMD17 + FIFO drain.
 *
 * No partition scanning, no MMC GPP partition switching, no extended
 * CSD parsing beyond capacity, no erase, no write.
 *
 * Reference: bootrom RE in OpenIPC/openhisilicon (bootrom/hi3516av300/re).
 */
#ifndef AGENT_EMMC_HIMCI_H
#define AGENT_EMMC_HIMCI_H

#include <stdint.h>

#ifdef EMMC_BASE

/* CSD-derived capacity. Updated by emmc_init() on success; 0 otherwise. */
extern uint64_t emmc_capacity_bytes;

/* eMMC card identification (first 16 bytes of CID). */
extern uint8_t emmc_cid[16];

/* Bring up the controller + card. Returns 0 on success, negative on failure. */
int emmc_init(void);

/* Read a single 512-byte block at `block_no` (LBA) into `dst`.
 * Returns 0 on success, negative on failure. */
int emmc_read_block(uint32_t block_no, uint8_t *dst);

#endif  /* EMMC_BASE */
#endif  /* AGENT_EMMC_HIMCI_H */
