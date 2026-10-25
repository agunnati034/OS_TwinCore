ROOT := $(abspath ..)
PROJECT_DIR := $(abspath .)
UPSTREAM_DIR := $(ROOT)/.deps/freertos
UPSTREAM_COMMIT := f4fcc3b228643144727e9257ba12db1cb632b6e6
UPSTREAM_MAKE ?= gmake
PYTHON ?= python3
DEMO_DIR := $(UPSTREAM_DIR)/FreeRTOS/Demo/RISC-V_RV32_QEMU_VIRT_GCC
APP_BUILD_DIR := $(PROJECT_DIR)/build/demo
TOOLCHAIN_PREFIX ?= riscv64-unknown-elf-
CC := $(TOOLCHAIN_PREFIX)gcc
SIZE := $(TOOLCHAIN_PREFIX)size
QEMU ?= qemu-system-riscv32
IMAGE := $(DEMO_DIR)/build/gcc/output/RTOSDemo.elf
APP_MAKEFILE := $(APP_BUILD_DIR)/Makefile

.PHONY: all deps prepare build run test-recovery clean check-compiler check-qemu

all: build

deps:
	@if [ ! -d "$(UPSTREAM_DIR)/.git" ]; then \
		mkdir -p "$(dir $(UPSTREAM_DIR))"; \
		git clone --filter=blob:none --no-checkout https://github.com/FreeRTOS/FreeRTOS.git "$(UPSTREAM_DIR)"; \
	fi
	git -C "$(UPSTREAM_DIR)" checkout --detach "$(UPSTREAM_COMMIT)"
	git -C "$(UPSTREAM_DIR)" submodule update --init FreeRTOS/Source

check-compiler:
	@command -v "$(CC)" >/dev/null || { echo "Missing cross-compiler: $(CC)" >&2; exit 1; }
	@command -v "$(SIZE)" >/dev/null || { echo "Missing size tool: $(SIZE)" >&2; exit 1; }

prepare:
	@test -f "$(DEMO_DIR)/build/gcc/Makefile" || { echo "FreeRTOS dependency missing; run 'make -C embedded deps'." >&2; exit 1; }
	mkdir -p "$(APP_BUILD_DIR)"
	sed -e 's/^SOURCE_FILES += (COMMON_DEMO_FILES)/SOURCE_FILES += $$(COMMON_DEMO_FILES)/' \
		-e 's/^SOURCE_FILES += (DEMO_PROJECT)/SOURCE_FILES += $$(DEMO_PROJECT)/' \
		"$(DEMO_DIR)/build/gcc/Makefile" > "$(APP_MAKEFILE)"
	ln -sf "$(DEMO_DIR)/main.c" "$(APP_BUILD_DIR)/main.c"
	ln -sf "$(PROJECT_DIR)/main_blinky.c" "$(APP_BUILD_DIR)/main_blinky.c"
	ln -sf "$(PROJECT_DIR)/main_full.c" "$(APP_BUILD_DIR)/main_full.c"
	ln -sf "$(DEMO_DIR)/FreeRTOSConfig.h" "$(APP_BUILD_DIR)/FreeRTOSConfig.h"
	ln -sf "$(DEMO_DIR)/ns16550.c" "$(APP_BUILD_DIR)/ns16550.c"
	ln -sf "$(DEMO_DIR)/ns16550.h" "$(APP_BUILD_DIR)/ns16550.h"
	ln -sf "$(DEMO_DIR)/riscv-virt.c" "$(APP_BUILD_DIR)/riscv-virt.c"
	ln -sf "$(DEMO_DIR)/riscv-virt.h" "$(APP_BUILD_DIR)/riscv-virt.h"
	ln -sf "$(DEMO_DIR)/riscv-reg.h" "$(APP_BUILD_DIR)/riscv-reg.h"
	ln -sf "$(DEMO_DIR)/vector.S" "$(APP_BUILD_DIR)/vector.S"

check-qemu:
	@command -v "$(QEMU)" >/dev/null || { echo "Missing QEMU system emulator: $(QEMU)" >&2; exit 1; }

build: check-compiler prepare
	$(MAKE) -C "$(DEMO_DIR)/build/gcc" -f "$(APP_MAKEFILE)" \
		DEMO_PROJECT="$(APP_BUILD_DIR)" \
		MAKE="$(UPSTREAM_MAKE)" \
		CC="$(CC)" LD="$(CC)" SIZE="$(SIZE)"

run: build check-qemu
	"$(QEMU)" -nographic -machine virt -net none -chardev stdio,id=con,mux=on \
		-serial chardev:con -mon chardev=con,mode=readline -bios none -smp 1 \
		-kernel "$(IMAGE)"

test-recovery: build check-qemu
	PYTHONPATH="$(ROOT)/src" "$(PYTHON)" -m unittest discover \
		-s "$(ROOT)/tests" -p "test_embedded_recovery_qemu.py" -v

clean:
	@if [ -d "$(DEMO_DIR)/build/gcc" ]; then \
		$(MAKE) -C "$(DEMO_DIR)/build/gcc" -f "$(APP_MAKEFILE)" clean MAKE="$(UPSTREAM_MAKE)"; \
	fi
	rm -rf "$(APP_BUILD_DIR)"
