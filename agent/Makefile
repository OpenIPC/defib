# defib flash agent — bare-metal ARM32 build
#
# Usage:
#   make                    # Build for hi3516ev300 (default)
#   make SOC=hi3516cv300    # Build for hi3516cv300
#   make clean

CROSS  = arm-none-eabi-
CC     = $(CROSS)gcc
AS     = $(CROSS)as
LD     = $(CROSS)ld
OBJCOPY = $(CROSS)objcopy
SIZE   = $(CROSS)size

# SoC-specific settings
SOC ?= hi3516ev300

# SoC configurations: UART_BASE, UART_CLOCK, LOAD_ADDR, FLASH_BASE, RAM_BASE
# SoC configurations from qemu-hisilicon/hw/arm/hisilicon.c
# FLASH_MEM = memory-mapped flash read window (fmc_mem_base)
# FMC_BASE  = FMC controller registers (fmc_ctrl_base)
ifeq ($(SOC),hi3516ev300)
  UART_BASE      = 0x12040000
  UART_CLOCK     = 24000000
  LOAD_ADDR      = 0x41000000
  FLASH_MEM      = 0x14000000
  FMC_BASE       = 0x10000000
  RAM_BASE       = 0x40000000
  WDT_BASE       = 0x12030000
  CRG_BASE       = 0x12010000
  SYSCTRL_REBOOT = 0x12020004
else ifeq ($(SOC),hi3516ev200)
  UART_BASE      = 0x12040000
  UART_CLOCK     = 24000000
  LOAD_ADDR      = 0x41000000
  FLASH_MEM      = 0x14000000
  FMC_BASE       = 0x10000000
  RAM_BASE       = 0x40000000
  WDT_BASE       = 0x12030000
  CRG_BASE       = 0x12010000
  SYSCTRL_REBOOT = 0x12020004
else ifeq ($(SOC),gk7205v200)
  UART_BASE      = 0x12040000
  UART_CLOCK     = 24000000
  LOAD_ADDR      = 0x41000000
  FLASH_MEM      = 0x14000000
  FMC_BASE       = 0x10000000
  RAM_BASE       = 0x40000000
  WDT_BASE       = 0x12030000
  CRG_BASE       = 0x12010000
  SYSCTRL_REBOOT = 0x12020004
else ifeq ($(SOC),gk7205v300)
  UART_BASE      = 0x12040000
  UART_CLOCK     = 24000000
  LOAD_ADDR      = 0x41000000
  FLASH_MEM      = 0x14000000
  FMC_BASE       = 0x10000000
  RAM_BASE       = 0x40000000
  WDT_BASE       = 0x12030000
  CRG_BASE       = 0x12010000
  SYSCTRL_REBOOT = 0x12020004
else ifeq ($(SOC),hi3516cv300)
  # V3 generation: ARM926EJ-S (ARMv5TEJ), not Cortex-A7.
  # UART is PL011 (same as ev300 family) at a different base.
  CPU_TYPE       = arm926ej-s
  UART_BASE      = 0x12100000
  UART_CLOCK     = 24000000
  LOAD_ADDR      = 0x81000000
  FLASH_MEM      = 0x14000000
  FMC_BASE       = 0x10000000
  RAM_BASE       = 0x80000000
  WDT_BASE       = 0x12080000
  CRG_BASE       = 0x12010000
  SYSCTRL_REBOOT = 0x12020004
else ifeq ($(SOC),hi3516cv500)
  # cv500-family (cv500, av300, dv300) — Cortex-A7. Memory map per
  # qemu-hisilicon hi3516cv500_soc; UART0 is at 0x120A0000, not the
  # 0x12100000 used by V3 (cv300) or hi3518ev200.
  UART_BASE      = 0x120A0000
  UART_CLOCK     = 24000000
  LOAD_ADDR      = 0x81000000
  FLASH_MEM      = 0x14000000
  FMC_BASE       = 0x10000000
  RAM_BASE       = 0x80000000
  WDT_BASE       = 0x12051000
  CRG_BASE       = 0x12010000
  SYSCTRL_REBOOT = 0x12020004
else ifeq ($(SOC),hi3518ev200)
  UART_BASE      = 0x12100000
  UART_CLOCK     = 24000000
  LOAD_ADDR      = 0x81000000
  FLASH_MEM      = 0x14000000
  FMC_BASE       = 0x10000000
  RAM_BASE       = 0x80000000
  WDT_BASE       = 0x20040000
  CRG_BASE       = 0x12010000
  SYSCTRL_REBOOT = 0x12020004
else ifeq ($(SOC),hi3516cv610)
  UART_BASE      = 0x11040000
  UART_CLOCK     = 24000000
  LOAD_ADDR      = 0x41000000
  FLASH_MEM      = 0x14000000
  FMC_BASE       = 0x10000000
  RAM_BASE       = 0x40000000
  WDT_BASE       = 0x12030000
  CRG_BASE       = 0x12010000
  SYSCTRL_REBOOT = 0x12020004
else ifeq ($(SOC),hi3519v101)
  # V3A generation (3519v101 family: hi3519v101, hi3516av200) — Cortex-A7
  # with V3-era peripheral addresses (UART 0x12100000, WDT 0x12080000)
  # but DDR at 0x80000000 like cv500-family. Memory map per
  # qemu-hisilicon hi3519v101_soc.
  UART_BASE      = 0x12100000
  UART_CLOCK     = 24000000
  LOAD_ADDR      = 0x81000000
  FLASH_MEM      = 0x14000000
  FMC_BASE       = 0x10000000
  RAM_BASE       = 0x80000000
  WDT_BASE       = 0x12080000
  CRG_BASE       = 0x12010000
  SYSCTRL_REBOOT = 0x12020004
else ifeq ($(SOC),hi3520dv200)
  # V1-era DVR/NVR generation — Cortex-A9 single-core, 0x2xxxxxxx peripheral
  # map, HISFC350 SPI controller (NOT the FMC100 used by V3+/V4+/V5/V6).
  # UART_CLOCK = CFG_CLK_BUS / 2 (per vendor Linux mach-godarm/core.c
  # uart_clk_init: busclk=396 MHz / 2 = 198 MHz). The bootrom hands us a
  # UART running off a slow ~2 MHz reference (IBRD=1, FBRD=5), so uart_init
  # must first clear UART_CKSEL bit 13 of CRG+0xE4 to switch UART to APB
  # clock — only then can we hit baud rates above 115200.
  # FLASH_MEM at 0x58000000 per vendor mach-godarm SPI_MEM_BASE;
  # FMC_BASE at 0x10010000 (HISFC350 register block).
  CPU_TYPE       = cortex-a9
  UART_BASE      = 0x20080000
  # The bootrom hands UART running off a slow ~2 MHz reference clock
  # (IBRD=1 / FBRD=5 → 115200 baud). uart_init preserves those divisors
  # so 115200 keeps working without any CRG poking. set_baud above
  # 115200 cannot succeed at this clock (would need IBRD<1) and is
  # rejected by the IBRD safety check. Switching to APB via vendor's
  # `CRG+0xE4 bit 13` produced an unidentified clock rate that we
  # couldn't decode at any standard baud — see GitHub issue tracking
  # the high-baud TODO. UART_CLOCK is the vendor U-Boot value
  # (CFG_CLK_BUS/4 per include/configs/hi3520d.h) but is unused while
  # the bootrom-divisor preservation path is active.
  UART_CLOCK     = 99000000
  LOAD_ADDR      = 0x81000000
  FLASH_MEM      = 0x58000000
  FMC_BASE       = 0x10010000
  RAM_BASE       = 0x80000000
  WDT_BASE       = 0x20040000
  CRG_BASE       = 0x20030000
  SYSCTRL_REBOOT = 0x20050004
  SPI_DRIVER     = hisfc350
else
  $(error Unknown SOC: $(SOC). Supported: hi3516ev300 hi3516ev200 gk7205v200 gk7205v300 hi3516cv300 hi3516cv500 hi3518ev200 hi3516cv610 hi3519v101 hi3520dv200)
endif

# Per-SoC CPU. V3 chips (cv300) are ARM926EJ-S (ARMv5TEJ);
# V4+ / cv500-family / V5 / V6 are Cortex-A7 (ARMv7-A).
CPU_TYPE ?= cortex-a7
ifeq ($(CPU_TYPE),arm926ej-s)
  CPU_FLAG = -DCPU_ARM926
else
  CPU_FLAG =
endif

CFLAGS = -mcpu=$(CPU_TYPE) -marm -O2 -ffreestanding -nostdlib \
         -DUART_BASE=$(UART_BASE) -DUART_CLOCK=$(UART_CLOCK) $(CPU_FLAG) \
         -DFLASH_MEM=$(FLASH_MEM) -DFMC_BASE=$(FMC_BASE) -DRAM_BASE=$(RAM_BASE) -DWDT_BASE=$(WDT_BASE) \
         -DCRG_BASE=$(CRG_BASE) -DSYSCTRL_REBOOT=$(SYSCTRL_REBOOT) \
         $(if $(UART_CKSEL_REG),-DUART_CKSEL_REG=$(UART_CKSEL_REG) -DUART_CKSEL_BIT=$(UART_CKSEL_BIT)) \
         -mno-unaligned-access -Wall -Wextra

LDFLAGS = -nostdlib -T link.ld -Ttext=$(LOAD_ADDR) --defsym=LOAD_ADDR=$(LOAD_ADDR)

# libgcc provides ARMv5 software-divide (__aeabi_uidiv etc) — required for
# ARM926 since it has no hardware UDIV/SDIV. Cortex-A7 has hardware divide
# but linking it costs nothing.
LIBGCC := $(shell $(CC) -mcpu=$(CPU_TYPE) -print-libgcc-file-name)

# SPI flash driver — default FMC100 (V3+/V4+/V5/V6 chips). V1-era chips
# (hi3520dv200, hi3518ev100, hi3516cv100, hi3535) need HISFC350 instead.
# Per-SoC stanza above sets SPI_DRIVER when needed.
SPI_DRIVER ?= fmc100
ifeq ($(SPI_DRIVER),fmc100)
  SPI_FLASH_SRC = spi_flash.c
else
  SPI_FLASH_SRC = spi_flash_$(SPI_DRIVER).c
endif

SRCS_C = main.c uart.c protocol.c cobs.c $(SPI_FLASH_SRC)
SRCS_S = startup.S
OBJS = $(SRCS_S:.S=.o) $(SRCS_C:.c=.o)

TARGET = agent-$(SOC)

all: $(TARGET).bin

$(TARGET).elf: $(OBJS)
	$(LD) $(LDFLAGS) -o $@ $^ $(LIBGCC)
	$(SIZE) $@

$(TARGET).bin: $(TARGET).elf
	$(OBJCOPY) -O binary $< $@
	@echo "Built $(TARGET).bin ($$(wc -c < $@) bytes)"

%.o: %.S
	$(CC) $(CFLAGS) -c -o $@ $<

%.o: %.c
	$(CC) $(CFLAGS) -c -o $@ $<

clean:
	rm -f *.o *.elf *.bin test_agent

# Build all supported SoCs
all-socs:
	$(MAKE) clean && $(MAKE) SOC=hi3516ev300
	$(MAKE) clean && $(MAKE) SOC=hi3516cv300
	$(MAKE) clean && $(MAKE) SOC=gk7205v200
	$(MAKE) clean && $(MAKE) SOC=hi3516cv610

# Host-native unit tests (not cross-compiled)
test: test_agent.c cobs.c protocol.c
	$(HOST_CC) -Wall -Wextra -O2 -DUART_BASE=0 -DUART_CLOCK=24000000 \
		-o test_agent test_agent.c cobs.c protocol.c
	./test_agent

HOST_CC ?= gcc

.PHONY: all clean all-socs test
