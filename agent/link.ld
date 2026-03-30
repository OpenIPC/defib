/*
 * Linker script for defib flash agent.
 * Loaded to DDR by the HiSilicon boot protocol.
 *
 * LOAD_ADDR is set via -Ttext= on the command line
 * (varies per SoC: 0x41000000 for ev300, 0x81000000 for cv300)
 */

ENTRY(_start)

SECTIONS {
    . = LOAD_ADDR;

    .text : {
        *(.text.start)
        *(.text*)
    }

    .rodata : {
        *(.rodata*)
    }

    .data : {
        *(.data*)
    }

    .bss : {
        __bss_start = .;
        *(.bss*)
        *(COMMON)
        __bss_end = .;
    }
}
