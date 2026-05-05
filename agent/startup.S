/*
 * defib flash agent — ARM32 bare-metal entry point
 *
 * Uploaded via HiSilicon boot protocol to DDR.
 * DDR and clocks are already initialized by the bootrom + DDR step.
 *
 * Both CPU paths set up identity-mapped MMU with write-back D-cache and
 * I-cache enabled.  DDR is cached, everything else is device-mapped
 * (uncached).  The page-table format differs between ARMv5 and ARMv7
 * but the layout (4096 × 1 MB sections, 16 KB-aligned) is identical.
 */

    .section .text.start
    .global _start
    .arm

/*
 * ARM exception vector table.  Must be at the start of the image because
 * the bootrom/SPL jumps to LOAD_ADDR (= _start) and we want a `b _start_code`
 * as the first instruction.
 */
_start:
_vectors:
    b       _start_code         /* Reset */
    b       _exc_hang           /* Undefined instruction */
    b       _exc_hang           /* SWI */
    b       _exc_hang           /* Prefetch abort */
#ifdef CPU_ARM926
    b       _exc_hang           /* Data abort (no fancy handler on ARMv5) */
#else
    b       _data_abort         /* Data abort */
#endif
    b       _exc_hang           /* Reserved */
    b       _exc_hang           /* IRQ */
    b       _exc_hang           /* FIQ */

_exc_hang:
    b       _exc_hang

#ifndef CPU_ARM926
/*
 * Data abort handler — prints fault info over UART before hanging.
 * ARMv7-only (uses `cps #imm`, ARM926 has no equivalent encoding here).
 */
    .global _data_abort
_data_abort:
    sub     lr, lr, #8
    mrc     p15, 0, r0, c6, c0, 0  /* r0 = DFAR */
    mrc     p15, 0, r1, c5, c0, 0  /* r1 = DFSR */
    mov     r2, lr                  /* r2 = faulting PC */
    cps     #0x13                   /* SVC mode (valid stack) */
    push    {r0-r3, lr}
    bl      data_abort_handler
    pop     {r0-r3, lr}
    b       _exc_hang
#endif

_start_code:
#ifdef CPU_ARM926
    /* =========================================================
     * ARM926EJ-S (ARMv5TEJ) — hi3516cv300
     *
     * No VBAR (vectors are at 0x00000000 or 0xFFFF0000 per CP15 c1.V).
     * No `dsb`/`isb`/`cps` mnemonics; ARMv5 uses CP15 ops and explicit
     * MSR for mode switches.  Section-descriptor format is simpler than
     * ARMv7: no XN, no TEX.  We map identity, DDR cached, rest device.
     * ========================================================= */

    /* Switch to SVC mode and mask IRQ + FIQ (ARMv5 MSR form) */
    mrs     r0, cpsr
    bic     r0, r0, #0x1F           /* clear mode bits */
    orr     r0, r0, #0x13           /* SVC */
    orr     r0, r0, #0xC0           /* mask IRQ + FIQ */
    msr     cpsr_c, r0

    /* Force MMU/caches off (clean state for setup) */
    mrc     p15, 0, r0, c1, c0, 0
    bic     r0, r0, #0x1            /* M = 0 (MMU off) */
    bic     r0, r0, #0x4            /* C = 0 (D-cache off) */
    bic     r0, r0, #0x1000         /* I = 0 (I-cache off) */
    mcr     p15, 0, r0, c1, c0, 0

    /* Invalidate I+D cache and TLB (ARMv5 ops) */
    mov     r0, #0
    mcr     p15, 0, r0, c7, c7, 0   /* invalidate I+D cache */
    mcr     p15, 0, r0, c8, c7, 0   /* invalidate TLB */
    mcr     p15, 0, r0, c7, c10, 4  /* drain write buffer (ARMv5 DSB) */

    /* Set up stack pointer (16KB below _start) */
    ldr     sp, =_start
    sub     sp, sp, #0x4000

    /* Clear BSS — includes the 16KB page table.  Must finish before MMU
     * enable so D-cache doesn't end up holding lines that mismatch the
     * page table writes below. */
    ldr     r0, =__bss_start
    ldr     r1, =__bss_end
    mov     r2, #0
bss_loop_v5:
    cmp     r0, r1
    strlt   r2, [r0], #4
    blt     bss_loop_v5

    /* ===== ARMv5 short-descriptor page table =====
     *
     * Section descriptor bits:
     *   [1:0]   = 0b10   (section)
     *   [2]     = B (bufferable)
     *   [3]     = C (cacheable)
     *   [8:5]   = Domain 0
     *   [11:10] = AP[1:0] = 0b11 (full access, no permission check)
     *   [31:20] = physical section base
     *
     * No XN, no TEX (ARMv7 additions).
     *
     * DEVICE (uncached I/O):
     *   AP=11, B=0, C=0  → (3<<10) | 0b10 = 0x00000C02
     * CACHED (DDR, write-back+bufferable):
     *   AP=11, B=1, C=1  → (3<<10) | (1<<3) | (1<<2) | 0b10 = 0x00000C0E
     */

    ldr     r4, =_page_table

    /* Fill all 4096 entries as DEVICE (uncached, identity-mapped) */
    ldr     r5, =0x00000C02
    mov     r0, r4
    mov     r1, r5
    mov     r7, #4096
pt_fill_device_v5:
    str     r1, [r0], #4
    add     r1, r1, #0x100000
    subs    r7, r7, #1
    bne     pt_fill_device_v5

    /* Mark 128 MB starting at RAM_BASE as CACHED. */
    ldr     r6, =0x00000C0E         /* CACHED descriptor template */
    ldr     r1, =RAM_BASE
    lsr     r2, r1, #20             /* r2 = first DDR section number */
    add     r0, r4, r2, lsl #2      /* r0 = &page_table[first_section] */
    orr     r1, r6, r2, lsl #20     /* r1 = descriptor for first DDR section */
    mov     r7, #128
pt_fill_ddr_v5:
    str     r1, [r0], #4
    add     r1, r1, #0x100000
    subs    r7, r7, #1
    bne     pt_fill_ddr_v5

    /* Drain write buffer so the page table writes are visible to the MMU
     * before TTBR points at it. */
    mov     r0, #0
    mcr     p15, 0, r0, c7, c10, 4  /* DSB equivalent */
    mcr     p15, 0, r0, c8, c7, 0   /* invalidate TLB */

    /* TTBR0 = page table base.  ARMv5 has no IRGN/RGN bits. */
    ldr     r0, =_page_table
    mcr     p15, 0, r0, c2, c0, 0

    /* DACR = all domains manager (skip permission checks) */
    mvn     r0, #0
    mcr     p15, 0, r0, c3, c0, 0

    /* Enable MMU + D-cache + I-cache via SCTLR */
    mrc     p15, 0, r0, c1, c0, 0
    orr     r0, r0, #0x1            /* M = MMU enable */
    orr     r0, r0, #0x4            /* C = D-cache enable */
    orr     r0, r0, #0x1000         /* I = I-cache enable */
    mcr     p15, 0, r0, c1, c0, 0
    /* Architecturally-defined NOPs to flush prefetch after enable */
    mov     r0, r0
    mov     r0, r0
    mov     r0, r0

    /* Jump to C main */
    bl      main

hang_v5:
    b       hang_v5

#else
    /* =========================================================
     * Cortex-A7 / ARMv7-A — ev300/cv500/cv610/gk7205/...
     * ========================================================= */

    /* Set VBAR to our vector table */
    adr     r0, _start
    mcr     p15, 0, r0, c12, c0, 0
    isb

    /* Disable interrupts */
    mrs     r0, cpsr
    orr     r0, r0, #0xC0
    msr     cpsr_c, r0

    /* Disable MMU/caches first (clean state for setup) */
    mov     r0, #0
    mcr     p15, 0, r0, c8, c7, 0  /* Invalidate TLB */
    mcr     p15, 0, r0, c7, c5, 0  /* Invalidate I-cache */
    mcr     p15, 0, r0, c7, c5, 6  /* Invalidate branch predictor */
    dsb
    isb

    mrc     p15, 0, r0, c1, c0, 0  /* Read SCTLR */
    bic     r0, r0, #0x1           /* Disable MMU */
    bic     r0, r0, #0x4           /* Disable D-cache */
    bic     r0, r0, #0x1000        /* Disable I-cache */
    mcr     p15, 0, r0, c1, c0, 0
    dsb
    isb

    /* Set up stack pointer (16KB below _start) */
    ldr     sp, =_start
    sub     sp, sp, #0x4000

    /* Clear BSS (includes page table) */
    ldr     r0, =__bss_start
    ldr     r1, =__bss_end
    mov     r2, #0
bss_loop:
    cmp     r0, r1
    strlt   r2, [r0], #4
    blt     bss_loop

    /* ===== ARMv7 short-descriptor page table =====
     *
     * Section descriptor bits:
     *   [1:0]   = 0b10   (section)
     *   [2]     = B (bufferable)
     *   [3]     = C (cacheable)
     *   [4]     = XN (execute never)
     *   [8:5]   = Domain 0
     *   [11:10] = AP = 0b11 (full access)
     *   [14:12] = TEX
     *   [31:20] = physical section base
     *
     * DEVICE (strongly-ordered, XN):
     *   AP=11, XN=1, B=0, C=0, TEX=000  → (3<<10)|(1<<4)|0b10 = 0x00000C12
     * CACHED (write-back, write-allocate):
     *   AP=11, XN=0, B=1, C=1, TEX=001  → (3<<10)|(1<<12)|(1<<3)|(1<<2)|0b10 = 0x00001C0E
     */

    ldr     r4, =_page_table

    /* Fill all 4096 entries as DEVICE (uncached) */
    ldr     r5, =0x00000C12        /* DEVICE descriptor */
    mov     r0, r4                 /* r0 = current entry pointer */
    mov     r1, r5                 /* r1 = descriptor for section 0 */
    mov     r7, #4096
pt_fill_device:
    str     r1, [r0], #4
    add     r1, r1, #0x100000     /* Next 1MB section */
    subs    r7, r7, #1
    bne     pt_fill_device

    /* Mark DDR sections as CACHED.
     * RAM_BASE is set at compile time (0x40000000 or 0x80000000).
     * We cache 128MB = 128 sections starting at RAM_BASE >> 20. */
    ldr     r6, =0x00001C0E        /* CACHED descriptor */
    ldr     r1, =RAM_BASE
    lsr     r2, r1, #20           /* r2 = first DDR section number */
    add     r0, r4, r2, lsl #2   /* r0 = &page_table[first_section] */
    orr     r1, r6, r2, lsl #20  /* r1 = descriptor for first DDR section */
    mov     r7, #128              /* 128 sections = 128MB */
pt_fill_ddr:
    str     r1, [r0], #4
    add     r1, r1, #0x100000
    subs    r7, r7, #1
    bne     pt_fill_ddr

    /* ===== ENABLE MMU + CACHES ===== */

    /* Invalidate TLB again (page table just written) */
    mov     r0, #0
    mcr     p15, 0, r0, c8, c7, 0
    dsb
    isb

    /* Set TTBR0 = page_table | inner WB/WA | outer WB/WA */
    ldr     r0, =_page_table
    orr     r0, r0, #(1 << 0)     /* IRGN bit 0 (inner WB) */
    orr     r0, r0, #(1 << 6)     /* IRGN bit 6 */
    orr     r0, r0, #(1 << 3)     /* RGN = outer WB */
    mcr     p15, 0, r0, c2, c0, 0
    isb

    /* DACR = all domains manager (no permission checks) */
    mvn     r0, #0
    mcr     p15, 0, r0, c3, c0, 0
    isb

    /* Enable MMU + D-cache + I-cache via SCTLR */
    mrc     p15, 0, r0, c1, c0, 0
    orr     r0, r0, #(1 << 0)     /* M = MMU enable */
    orr     r0, r0, #(1 << 2)     /* C = D-cache enable */
    orr     r0, r0, #(1 << 12)    /* I = I-cache enable */
    mcr     p15, 0, r0, c1, c0, 0
    dsb
    isb

    /* Jump to C main */
    bl      main

hang:
    wfi
    b       hang
#endif

/* ===== PAGE TABLE (in BSS, 16 KB aligned) — shared by both CPU paths ===== */
    .section .bss
    .align 14
    .global _page_table
_page_table:
    .space 16384
