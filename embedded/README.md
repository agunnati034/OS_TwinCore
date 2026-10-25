# FreeRTOS QEMU task prototype

This is an independent embedded prototype; it does not change the Python
host-process telemetry or dashboard.

## Target and upstream sources

The target is FreeRTOS's upstream **RISC-V RV32 QEMU virt** demo, using
QEMU's `virt` machine, the RV32I CLINT timer extension configuration and its
UART serial console. This is an existing FreeRTOS demo/port pairing, not a
custom QEMU machine:

- [FreeRTOS upstream demo and build instructions](https://github.com/FreeRTOS/FreeRTOS/tree/f4fcc3b228643144727e9257ba12db1cb632b6e6/FreeRTOS/Demo/RISC-V_RV32_QEMU_VIRT_GCC)
- [FreeRTOS RISC-V GCC port](https://github.com/FreeRTOS/FreeRTOS-Kernel/tree/main/portable/GCC/RISC-V)
- [QEMU RISC-V `virt` machine](https://www.qemu.org/docs/master/system/riscv/virt.html)

The FreeRTOS distribution checkout is pinned to commit
`f4fcc3b228643144727e9257ba12db1cb632b6e6` (2026-08-26). `make deps` checks
out that revision and initializes only its recorded FreeRTOS kernel
submodule; unrelated distribution submodules are not fetched. The demo board
support, linker/startup files, and kernel port are taken from that upstream
checkout. No kernel source is copied into this project.

The demo uses the board support's NS16550 UART output and the CLINT-backed
tick configuration in the upstream `FreeRTOSConfig.h`. The upstream example
documents `qemu-system-riscv32 -machine virt` and the
`RV32I_CLINT_no_extensions` port configuration. This firmware has been built
and run on this Apple Silicon Mac with the upstream target.

## macOS prerequisites

The selected machine architecture is RV32 even though the development Mac is
Apple Silicon. Native Apple Clang cannot compile the FreeRTOS RISC-V port.
QEMU and an RV32-capable bare-metal GCC toolchain are required; no physical
board is needed.

Homebrew provides QEMU:

```sh
brew install qemu
```

The Homebrew `riscv64-elf-gcc` formula targets `riscv64-elf` without the RV32
multilib required by this demo, so it is not the default recommended
toolchain. Use a RISC-V GNU Embedded toolchain that supports `rv32imac` and
`ilp32`; the upstream demo documents the SiFive GNU Embedded Toolchain. Set
`TOOLCHAIN_PREFIX` to the executable prefix. For a self-built toolchain,
the FreeRTOS demo documents the RISC-V GNU toolchain source revision below.
The following commands install build dependencies into the Homebrew prefix,
build the compiler from source into a user-owned directory, and do not use
`sudo`. The GNU toolchain source is pinned to
`a33dac0251d17a7b74d99bd8fd401bfce87d2aed`, the revision documented by this
FreeRTOS demo. The upstream instructions estimate about 8 GiB of disk for the build and a
roughly 6.65 GiB source checkout; keep additional free space available.

```sh
HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK=1 brew install \
  qemu python3 gawk gnu-sed make gmp mpfr libmpc isl \
  zlib expat texinfo flock libslirp
git clone https://github.com/riscv-collab/riscv-gnu-toolchain.git \
  "$HOME/src/riscv-gnu-toolchain"
cd "$HOME/src/riscv-gnu-toolchain"
git checkout a33dac0251d17a7b74d99bd8fd401bfce87d2aed
CPPFLAGS="-I$(brew --prefix)/include" \
LDFLAGS="-L$(brew --prefix)/lib" \
./configure \
  --prefix="$HOME/.local/riscv" \
  --enable-multilib \
  --disable-gdb
gmake -j2 BINUTILS_TARGET_FLAGS_EXTRA=--with-system-zlib
```

Then add the compiler to the current shell's PATH and verify its RV32
multilib:

```sh
export PATH="$HOME/.local/riscv/bin:$PATH"
riscv64-unknown-elf-gcc -print-multi-lib
```

The RISC-V GNU toolchain's macOS instructions specify using `gmake`; its
Makefile fetches source submodules on demand, so a recursive submodule clone is
unnecessary. GDB is disabled because the FreeRTOS demo does not use it. The
system-zlib option avoids a compatibility issue in the bundled zlib on current
macOS SDKs. Homebrew modifies its own prefix, so get explicit approval before
running the installation/build commands in a new environment.

Before building, verify the tools and target support:

```sh
qemu-system-riscv32 --version
export PATH="$HOME/.local/riscv/bin:$PATH"
riscv64-unknown-elf-gcc --version
riscv64-unknown-elf-gcc -print-multi-lib
```

The multilib output must include an RV32 `ilp32` variant supporting `rv32imac`.
Set the prefix below to match the directory where the toolchain was installed.

## Fetch, build, run

From the repository root:

```sh
export PATH="$HOME/.local/riscv/bin:$PATH"
make -C embedded deps
make -C embedded build
make -C embedded run
```

`prepare` creates ignored source links and a local copy of the upstream
Makefile so the build uses these application files without editing the pinned
checkout.

If the compiler uses a different executable prefix, pass it explicitly, for
example `make -C embedded build TOOLCHAIN_PREFIX=riscv64-elf-`.

`make run` runs QEMU interactively with serial output attached to the terminal.
Exit QEMU with **Ctrl-A**, then **X**. `make -C embedded clean` removes only
this project's generated application links and the upstream demo build
outputs; `make -C embedded deps` keeps the pinned checkout in the ignored
`.deps/freertos` directory.

The application writes a JSON boot record followed by records from both
`sensor-sampler` and `control-loop`. The two tasks use different periods (250
and 1000 FreeRTOS ticks) and different deterministic work loops. Output is
generated by each executing task over the emulated UART; it is not synthesized
by Python or the dashboard.

Captured output from a QEMU run of this firmware:

```json
{"schema_version":1,"message_type":"task_telemetry","target":"freertos-qemu-rv32","task_id":"sensor-sampler","task_state":"running","tick_count":0,"execution_count":1,"stack_high_water_mark_words":138}
{"schema_version":1,"message_type":"task_telemetry","target":"freertos-qemu-rv32","task_id":"control-loop","task_state":"running","tick_count":0,"execution_count":1,"stack_high_water_mark_words":138}
{"schema_version":1,"message_type":"task_telemetry","target":"freertos-qemu-rv32","task_id":"sensor-sampler","task_state":"running","tick_count":250,"execution_count":2,"stack_high_water_mark_words":135}
{"schema_version":1,"message_type":"task_telemetry","target":"freertos-qemu-rv32","task_id":"sensor-sampler","task_state":"running","tick_count":500,"execution_count":3,"stack_high_water_mark_words":135}
{"schema_version":1,"message_type":"task_telemetry","target":"freertos-qemu-rv32","task_id":"sensor-sampler","task_state":"running","tick_count":750,"execution_count":4,"stack_high_water_mark_words":135}
{"schema_version":1,"message_type":"task_telemetry","target":"freertos-qemu-rv32","task_id":"control-loop","task_state":"running","tick_count":1000,"execution_count":2,"stack_high_water_mark_words":135}
```

These are observed UART records captured from the rebuilt firmware. This run
produced four `sensor-sampler` and two `control-loop` samples; the counters
advanced while QEMU was running.

## Task telemetry contract

Each producer record is schema version 1 and contains these fields:

| Field | Meaning |
|---|---|
| `schema_version` | Message format version (`1`). |
| `message_type` | `task_telemetry` for task samples. |
| `target` | Target identifier `freertos-qemu-rv32`. |
| `task_id` | Application task identity (`sensor-sampler` or `control-loop`); not a PID. |
| `task_state` | State from `eTaskGetState(xTaskGetCurrentTaskHandle())` at emission. |
| `tick_count` | Current FreeRTOS tick count. |
| `execution_count` | Number of periodic iterations completed by this task. |
| `stack_high_water_mark_words` | Measured stack high-water mark in `StackType_t` words. |

`tick_count` comes from `xTaskGetTickCount()`,
`execution_count` is maintained by that FreeRTOS task,
`stack_high_water_mark_words` comes from
`uxTaskGetStackHighWaterMark(NULL)`, and task state is queried from the current
task's FreeRTOS handle. `task_id` is an application identifier, not a host PID
or an OS process ID. Stack high-water mark is reported in `StackType_t` words;
it is not a measurement of total free heap or CPU utilization.

## Forward telemetry to the dashboard

Start the existing API and Vite dashboard using the separate-terminal
instructions in the root README. From the project root, run:

```sh
export PATH="$HOME/.local/riscv/bin:$PATH"
.venv/bin/python -m twincoreos embedded-bridge
```

The bridge launches the QEMU process with `-serial stdio -monitor none
-display none`, reads complete JSON Lines from the NS16550 UART pipe, validates
the records, and POSTs them to
`http://127.0.0.1:8765/api/embedded/telemetry`. It creates a unique UUID for
each bridge/QEMU session so execution counters can restart on a later boot.
Malformed or incomplete lines are reported and discarded; backend HTTP
failures are explicit and stop the owned QEMU process. Ctrl-C shuts down that
QEMU process cleanly. The bridge needs no additional Python package.

The API validates the exact version-1 task schema and de-duplicates by
bridge-session UUID, task ID, and execution counter. `GET /api/embedded/tasks`
returns separate `latest` and bounded `history` arrays; the dashboard's
`GET /api/dashboard` response includes the same `embedded_tasks` object.
Accepted records are also added to the event log as
`embedded_task_telemetry`. Only RTOS-reported fields are shown for embedded
tasks; they are never added to host-process workers, CPU/RSS data, Digital
Twins, risks, checkpoints, or host recovery.

`GET /api/embedded/risk` returns a separate read-only heuristic operational-risk
assessment for each supported task identity. The policy uses the backend's
actual receipt time to mark telemetry stale and compares the measured
`stack_high_water_mark_words` with the configurable runtime policy threshold
(`embedded_stack_reserve_threshold_words`, default 32). A running task with a
fresh stack reserve at or below the threshold yields a
`checkpoint_recommended` assessment; stale, missing, malformed, or unsupported
identity data yields `risk_level: unknown` and no recommendation. This is not a
trained or calibrated failure predictor. Only `sensor-sampler` currently has a
supported application-state checkpoint record; `control-loop` assessments
therefore remain unknown with no checkpoint recommendation.

The bridge polls this endpoint after each accepted UART telemetry record. Only a
fresh `sensor-sampler` recommendation under the named stack-reserve rule sends
the exact line `CHECKPOINT sensor-sampler\n` through QEMU's serial stdio input.
Firmware polls the supported NS16550 UART receive/status registers and accepts
only that complete command. The recovery controller then uses its existing
task-notification safe point and versioned, integrity-checked application-state
record. Firmware lifecycle events travel outward over UART and are posted to
`POST /api/embedded/events`; they do not create host PID checkpoints. The bridge
guards duplicate requests and does not re-arm after success or failure until a
fresh normal assessment proves the stack-reserve condition has cleared. With
the default 32-word threshold this workload may never request a checkpoint;
for a demonstration, start `serve` with
`--embedded-stack-reserve-threshold-words 200` and let actual telemetry satisfy
the configured rule.

To verify, observe increasing `execution_count` and `tick_count` values for
both task IDs in the dashboard's **Embedded RTOS** section and the live event
log. Stop the bridge with Ctrl-C; its QEMU process exits without affecting the
host worker supervisor.

## Application-state checkpoint and restart demonstration

The firmware performs a controlled application-level restart of
`sensor-sampler` only after the bridge sends the policy-authorized UART
command. Its task reaches a loop safe point, saves its
own `counter` and `accumulator` in an embedded-only schema-v1 record, and
acknowledges the controller. The record includes a task identity and an FNV-1a
integrity value over the record version and saved state. The controller
validates the record, suspends the old task, creates a replacement, then
deletes the suspended task. If replacement creation fails, it resumes the
original task instead. The replacement emits restore confirmation and must
advance the restored counter and accumulator by exactly one workload iteration
before the controller reports completion.

`control-loop` is independent and continues to emit its own FreeRTOS telemetry
while the selected task is restarted. Lifecycle messages use
`message_type=task_event`; the bridge validates and forwards those events to
the separate embedded event-ingestion endpoint. They are not host-process
events or host checkpoints.

Run the reproducible integration test from the repository root:

```sh
export PATH="$HOME/.local/riscv/bin:$PATH"
make -C embedded test-recovery
```

The test builds the pinned firmware, runs the real QEMU `virt` target, and
checks the UART event order, checkpoint integrity, restored state, next
accumulator value, and continued `control-loop` telemetry. The independent
policy-driven integration test runs the real bridge and backend with a
configured stack threshold and verifies the live telemetry recommendation
causes the UART checkpoint event and validated restore. It requires the
documented RISC-V toolchain and `qemu-system-riscv32` on `PATH`. To inspect the
same actual event stream interactively, run `make -C embedded run` and watch
for `checkpoint_saved`, `task_restart_started`, `task_restarted`,
`restore_succeeded`, `task_progress`, and `recovery_completed`.

This demonstration checkpoints only application-level data held in a versioned
RAM record. The record does not survive QEMU reset or power loss. The
replacement starts with a new task stack and a new scheduling phase; no
arbitrary stack frame, register, peripheral, or kernel state is restored.
This is not FreeRTOS kernel-task-context recovery and is separate from the
host-process checkpoint manager.

## Troubleshooting

- `Missing cross-compiler` from `make build` or `Missing QEMU system emulator`
  from `make run`: install the prerequisites above and ensure the commands are
  on `PATH`; override `TOOLCHAIN_PREFIX` or `QEMU` for non-default locations.
- `can't link RV32` or ABI/multilib errors: ensure the GCC toolchain supports
  `-march=rv32imac -mabi=ilp32`; the Homebrew `riscv64-elf-gcc` formula alone
  is not sufficient.
- `FreeRTOS dependency missing`: run `make -C embedded deps` from a networked
  environment. The pinned source is stored under ignored `.deps/`.
- No serial output: use the documented `virt` machine, `-bios none`, one hart,
  and serial-console options from the `run` target. The expected JSON lines
  appear only after the scheduler starts both tasks.
- No embedded records in the dashboard: confirm the API is running on port
  8765, then start `twincoreos embedded-bridge` and check its stderr for
  forwarding or HTTP rejection messages. The bridge only runs while its QEMU
  child is alive.

## Scope and current verification state

This is an early task-scheduling and supported stack-usage measurement demo
with a controlled, single-task application-state checkpoint/restart
demonstration. It does not implement arbitrary task-stack restoration,
register/peripheral/kernel-state recovery or CPU utilization measurement. On
this Mac the pinned toolchain built successfully as GCC 14.2.0 with the
`rv32imac/ilp32` multilib, and QEMU 11.1.2 executed both tasks. The linked
artifact is an ELF32 RISC-V executable. The project bridge uses QEMU serial
stdio bidirectionally: it reads firmware UART output, posts telemetry and
lifecycle events to the backend, and writes a checkpoint command only after
a fresh embedded policy recommendation. The current demonstration verifies
application-state checkpointing and controlled task replacement/restore;
the checkpoint is RAM-only and is not a saved CPU context.
