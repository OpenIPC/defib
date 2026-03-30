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
  UART_BASE  = 0x12040000
  UART_CLOCK = 24000000
  LOAD_ADDR  = 0x41000000
  FLASH_MEM  = 0x14000000
  FMC_BASE   = 0x10000000
  RAM_BASE   = 0x40000000
  WDT_BASE   = 0x12030000
else ifeq ($(SOC),hi3516ev200)
  UART_BASE  = 0x12040000
  UART_CLOCK = 24000000
  LOAD_ADDR  = 0x41000000
  FLASH_MEM  = 0x14000000
  FMC_BASE   = 0x10000000
  RAM_BASE   = 0x40000000
  WDT_BASE   = 0x12030000
else ifeq ($(SOC),gk7205v200)
  UART_BASE  = 0x12040000
  UART_CLOCK = 24000000
  LOAD_ADDR  = 0x41000000
  FLASH_MEM  = 0x14000000
  FMC_BASE   = 0x10000000
  RAM_BASE   = 0x40000000
  WDT_BASE   = 0x12030000
else ifeq ($(SOC),gk7205v300)
  UART_BASE  = 0x12040000
  UART_CLOCK = 24000000
  LOAD_ADDR  = 0x41000000
  FLASH_MEM  = 0x14000000
  FMC_BASE   = 0x10000000
  RAM_BASE   = 0x40000000
  WDT_BASE   = 0x12030000
else ifeq ($(SOC),hi3516cv300)
  UART_BASE  = 0x12100000
  UART_CLOCK = 24000000
  LOAD_ADDR  = 0x81000000
  FLASH_MEM  = 0x14000000
  FMC_BASE   = 0x10000000
  RAM_BASE   = 0x80000000
  WDT_BASE   = 0x12080000
else ifeq ($(SOC),hi3516cv500)
  UART_BASE  = 0x12100000
  UART_CLOCK = 24000000
  LOAD_ADDR  = 0x81000000
  FLASH_MEM  = 0x14000000
  FMC_BASE   = 0x10000000
  RAM_BASE   = 0x80000000
  WDT_BASE   = 0x12051000
else ifeq ($(SOC),hi3518ev200)
  UART_BASE  = 0x12100000
  UART_CLOCK = 24000000
  LOAD_ADDR  = 0x81000000
  FLASH_MEM  = 0x14000000
  FMC_BASE   = 0x10000000
  RAM_BASE   = 0x80000000
  WDT_BASE   = 0x20040000
else ifeq ($(SOC),hi3516cv610)
  UART_BASE  = 0x11040000
  UART_CLOCK = 24000000
  LOAD_ADDR  = 0x41000000
  FLASH_MEM  = 0x14000000
  FMC_BASE   = 0x10000000
  RAM_BASE   = 0x40000000
  WDT_BASE   = 0x12030000
else
  $(error Unknown SOC: $(SOC). Supported: hi3516ev300 hi3516ev200 gk7205v200 gk7205v300 hi3516cv300 hi3516cv500 hi3518ev200 hi3516cv610)
endif

CFLAGS = -mcpu=cortex-a7 -marm -O2 -ffreestanding -nostdlib \
         -DUART_BASE=$(UART_BASE) -DUART_CLOCK=$(UART_CLOCK) \
         -DFLASH_MEM=$(FLASH_MEM) -DFMC_BASE=$(FMC_BASE) -DRAM_BASE=$(RAM_BASE) -DWDT_BASE=$(WDT_BASE) \
         -mno-unaligned-access -Wall -Wextra

LDFLAGS = -nostdlib -T link.ld -Ttext=$(LOAD_ADDR) --defsym=LOAD_ADDR=$(LOAD_ADDR)

SRCS_C = main.c uart.c protocol.c cobs.c spi_flash.c
SRCS_S = startup.S
OBJS = $(SRCS_S:.S=.o) $(SRCS_C:.c=.o)

TARGET = agent-$(SOC)

all: $(TARGET).bin

$(TARGET).elf: $(OBJS)
	$(LD) $(LDFLAGS) -o $@ $^
	$(SIZE) $@

$(TARGET).bin: $(TARGET).elf
	$(OBJCOPY) -O binary $< $@
	@echo "Built $(TARGET).bin ($$(wc -c < $@) bytes)"

%.o: %.S
	$(CC) $(CFLAGS) -c -o $@ $<

%.o: %.c
	$(CC) $(CFLAGS) -c -o $@ $<

clean:
	rm -f *.o *.elf *.bin

# Build all supported SoCs
all-socs:
	$(MAKE) clean && $(MAKE) SOC=hi3516ev300
	$(MAKE) clean && $(MAKE) SOC=hi3516cv300
	$(MAKE) clean && $(MAKE) SOC=gk7205v200
	$(MAKE) clean && $(MAKE) SOC=hi3516cv610

.PHONY: all clean all-socs
