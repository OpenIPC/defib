/*
 * defib flash agent — ARM32 bare-metal entry point
 *
 * Uploaded via HiSilicon boot protocol to DDR.
 * DDR and clocks are already initialized by the bootrom + DDR step.
 * We just need to set up the stack and jump to main().
 */

    .section .text.start
    .global _start
    .arm

/*
 * ARM exception vector table.
 * Must be at the entry point so VBAR points here.
 * All exceptions loop forever instead of resetting the SoC
 * (the SPL's handlers call reset_cpu which kills us).
 */
_start:
_vectors:
    b       _start_code         /* Reset */
    b       _exc_hang           /* Undefined instruction */
    b       _exc_hang           /* SWI */
    b       _exc_hang           /* Prefetch abort */
    b       _data_abort         /* Data abort */
    b       _exc_hang           /* Reserved */
    b       _exc_hang           /* IRQ */
    b       _exc_hang           /* FIQ */

_exc_hang:
    b       _exc_hang

/*
 * Data abort handler — prints fault info over UART before hanging.
 * On entry (ABT mode): LR_abt = faulting instruction + 8.
 * We must NOT use the stack (SP_abt is uninitialized).
 * Instead, read CP15 regs and call handler using SVC stack.
 */
    .global _data_abort
_data_abort:
    sub     lr, lr, #8              /* LR = faulting PC */

    /* Read fault info into r0-r2 (caller-saved, safe to clobber) */
    mrc     p15, 0, r0, c6, c0, 0  /* r0 = DFAR (fault address) */
    mrc     p15, 0, r1, c5, c0, 0  /* r1 = DFSR (fault status) */
    mov     r2, lr                  /* r2 = faulting PC */

    /* Switch to SVC mode (which has a valid stack) with IRQ/FIQ disabled */
    cps     #0x13                   /* SVC mode */

    push    {r0-r3, lr}
    bl      data_abort_handler
    pop     {r0-r3, lr}

    b       _exc_hang

_start_code:
    /* Set VBAR to our vector table (use ADR for PC-relative) */
    adr     r0, _start
    mcr     p15, 0, r0, c12, c0, 0  /* Write VBAR */
    isb

    /* Disable interrupts */
    mrs     r0, cpsr
    orr     r0, r0, #0xC0      /* Disable IRQ and FIQ */
    msr     cpsr_c, r0

    /* Invalidate TLBs, caches, then disable MMU */
    mov     r0, #0
    mcr     p15, 0, r0, c8, c7, 0  /* Invalidate entire TLB */
    mcr     p15, 0, r0, c7, c5, 0  /* Invalidate I-cache */
    mcr     p15, 0, r0, c7, c5, 6  /* Invalidate branch predictor */
    dsb
    isb

    mrc     p15, 0, r0, c1, c0, 0  /* Read SCTLR */
    bic     r0, r0, #0x1           /* Disable MMU */
    bic     r0, r0, #0x4           /* Disable D-cache */
    bic     r0, r0, #0x1000        /* Disable I-cache */
    mcr     p15, 0, r0, c1, c0, 0  /* Write SCTLR */
    dsb
    isb

    /* Set up stack pointer (16KB below _start) */
    ldr     sp, =_start
    sub     sp, sp, #0x4000     /* 16KB stack below code */

    /* Clear BSS */
    ldr     r0, =__bss_start
    ldr     r1, =__bss_end
    mov     r2, #0
bss_loop:
    cmp     r0, r1
    strlt   r2, [r0], #4
    blt     bss_loop

    /* Jump to C main */
    bl      main

    /* If main returns, loop forever */
hang:
    wfi
    b       hang
