# TwinCoreOS

TwinCoreOS is an OS/RTOS-focused fault-tolerance prototype. Its current
demonstration is a **host-process system**: a Python supervisor starts real
worker subprocesses, samples OS-reported measurements through `psutil`,
maintains per-worker Digital Twins, computes a baseline risk assessment,
stores validated application-state checkpoints, and demonstrates controlled
worker termination and application-state recovery through a local API and
React dashboard.

It is not a kernel or RTOS task monitor, a trained or calibrated failure
predictor, or arbitrary process-memory restoration. A separate RV32
FreeRTOS-on-QEMU task prototype is under `embedded/`; its UART telemetry can
be forwarded into a separate embedded-task view in the dashboard. Embedded
tasks remain distinct from supervised host processes and their Digital Twins.

## Prerequisites and project structure

Requirements for the host prototype:

- Python 3.10 or newer.
- Node.js and npm versions supported by the Vite version in
  `frontend/package-lock.json`.
- A local loopback interface with ports 8765 (API) and 5173 (Vite) available,
  or matching configuration changes to the API command, Vite server, and
  `VITE_API_URL`.

No Docker, C compiler, scikit-learn, or emulator is required for the current
host-process demonstration.

```text
src/twincoreos/       Worker, monitor, Digital Twin, risk, checkpoints, recovery, API
tests/                Unit tests and process/API integration tests
frontend/src/         React dashboard
frontend/package.json Frontend commands and dependencies
frontend/package-lock.json Locked frontend dependencies for reproducible npm ci
frontend/.env.example Frontend API URL template
pyproject.toml        Python package metadata and runtime dependency
```

## Fresh setup

From the project root, create an isolated Python environment and install the
package and its runtime dependency (`psutil`):

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
```

Install the frontend's locked dependencies:

```sh
cd frontend
npm ci
cp .env.example .env
cd ..
```

`frontend/.env.example` sets `VITE_API_URL=http://127.0.0.1:8765/api`. The
checked-in Vite configuration binds the frontend to `127.0.0.1:5173`; the API
defaults to `127.0.0.1:8765`. The `.env` file is local-only and ignored by Git.
Change `VITE_API_URL` only when the API address changes, and restart Vite after
editing it because Vite reads environment variables at startup.

## Start and stop the dashboard

Start these services in separate terminals, from the project root.

**Terminal 1 — backend API and supervised workers:**

```sh
.venv/bin/python -m twincoreos serve \
  --workers 2 \
  --memory-mib 8 \
  --sample-interval 1 \
  --checkpoint-interval 5 \
  --checkpoint-budget-bytes 10485760 \
  --checkpoint-dir .twincoreos/dashboard-checkpoints \
  --host 127.0.0.1 \
  --port 8765
```

The API starts one monitor runtime and one worker pool for its lifetime. Keep
this terminal running; dashboard polling and API reads do not start more
workers. To stop the backend, press **Ctrl-C in Terminal 1**. The service
closes the HTTP server and shuts down and reaps its supervised workers.

**Terminal 2 — React/Vite dashboard:**

```sh
cd frontend
npm run dev
```

Open <http://127.0.0.1:5173/>. To stop the frontend, press **Ctrl-C in
Terminal 2**. Stop the frontend and backend independently; stopping Vite does
not stop the backend. The API binds to loopback by default, has no
authentication, and should not be exposed to untrusted networks.

To confirm the API is ready and publishing workers, from another terminal run:

```sh
curl -fsS http://127.0.0.1:8765/api/status
curl -fsS http://127.0.0.1:8765/api/dashboard
```

The dashboard polls `/api/dashboard` once per second. It displays live
measurements from the backend; it does not substitute mock data when the API
is unavailable.

## Architecture and data flow

1. `worker.py` starts each supervised worker as a real child process. A worker
   reports readiness after initializing its configured memory and supported
   application workload state.
2. `monitor.py` samples OS process identity, status, CPU time/utilization, and
   RSS with `psutil`, while also reading the worker's explicitly supported
   application state over its control protocol.
3. The monitor updates `digital_twin.py` with observations to maintain a
   per-worker current/previous profile and health state. `risk_predictor.py`
   consumes those real observations and Twin state to produce a transparent
   baseline assessment.
4. `checkpoint_manager.py` validates, integrity-checks, and stores only the
   worker-exposed application state under the configured byte budget.
   `recovery.py` handles a confirmed fault request: it terminates only the
   selected supervisor-owned worker, selects a valid checkpoint, starts a
   fresh worker, restores and verifies state, then verifies a subsequent
   state advance.
5. `runtime.py` owns one monitor loop and coordinates telemetry, Twins, risk,
   checkpoints, recovery requests, and a bounded event history. `api.py`
   exposes that runtime through JSON HTTP endpoints; reads do not start
   workers or monitoring loops.
6. `frontend/src/` fetches the API snapshot and renders its actual worker,
   telemetry, Twin, risk, checkpoint, recovery, and event data. Browser fault
   requests call the selected worker's API route and display the returned
   recovery result; ordinary polling never injects faults.

## Standalone CLI and checks

To run the standalone telemetry CLI instead of the dashboard:

```sh
.venv/bin/python -m twincoreos monitor \
  --workers 2 --duration 10 --sample-interval 1 --memory-mib 8
```

Run all Python tests:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Build and type-check the frontend:

```sh
cd frontend
npm run build
npm run lint
```

After Python source or dependency metadata changes, reinstall with
`.venv/bin/python -m pip install .`.

The supervisor starts the requested Python worker subprocesses. Each worker
allocates the configured memory, touches every 4 KiB page, initializes its
workload state, and then writes a JSON `ready` message over a private pipe.
Telemetry sampling and the monitoring-duration clock start only after **all**
workers have reported ready. A worker's output pipe is separate from the
supervisor's JSON telemetry on stdout.

`--startup-timeout` is the maximum time allowed for all workers to become
ready. `--shutdown-timeout` is the per-worker graceful termination wait. On
normal exit, interruption, startup failure, or a telemetry error, the
supervisor sends termination to workers, waits up to that timeout for each,
then kills and waits for any worker that did not exit. Ctrl-C exits with status
130 and a short message, without a traceback; SIGTERM exits with status 143.
Signals are recorded by the supervisor and acted on at safe lifecycle
checkpoints, so an interrupt during process creation does not abandon a child
between launch and registration. Unexpected errors remain visible and produce
a nonzero exit.

## Telemetry schema and measurement definitions

Each JSON object on stdout is one worker observation:

| Field | Definition |
|---|---|
| `task_id` | Supervisor-assigned label such as `worker-1`; not a kernel task ID. |
| `pid`, `ppid` | Worker process ID and parent process ID reported by the OS. `ppid` is null only if unavailable, with a corresponding `measurement_errors` entry. |
| `process_created_at_utc` | OS process creation time converted to an ISO-8601 UTC timestamp. |
| `captured_at_utc` | Wall-clock UTC time at which this observation was assembled. |
| `monitor_elapsed_seconds` | Monotonic time since telemetry monitoring began, after all workers became ready. |
| `workload_elapsed_seconds` | Monotonic time since the worker completed memory initialization and workload setup. It is not process creation time. |
| `cpu_time_user_seconds_total`, `cpu_time_system_seconds_total`, `cpu_time_total_seconds` | OS-reported cumulative CPU time at collection, in seconds. Total is user plus system CPU time. |
| `cpu_utilization_percent_one_core` | CPU-time delta divided by the elapsed time between CPU samples, multiplied by 100. It is interval utilization, not cumulative CPU time or a prediction. `100%` means one logical CPU core fully occupied; multithreaded processes may exceed 100%. It is **not normalized to total system capacity**. |
| `cpu_interval_seconds` | Actual monotonic elapsed time used as the denominator for the CPU utilization observation, in seconds. |
| `cpu_utilization_scope` | Explicit normalization label: `one_logical_cpu_core`. |
| `memory_rss_bytes` | OS-reported resident set size in bytes. This includes interpreter/runtime memory as well as the worker's allocation. |
| `process_status` | OS-reported process status at collection, or null if unavailable. |
| `measurement_errors` | Map of unavailable metric names to error details. Affected values are null; no replacement measurements are fabricated. An empty object means no handled per-field access-denial errors occurred. |

The CPU baseline is collected after readiness. The first normal sample is not
emitted until one complete `--sample-interval` has elapsed, avoiding a
startup-sized CPU interval. Duration is measured from readiness and may end
before another full interval; in that case the incomplete final interval is
not emitted. `--sample-interval` must not exceed `--duration`.

## Digital Twin output and health rules

For every telemetry batch, the CLI first emits the existing `type: telemetry`
records, unchanged, then emits one `type: digital_twin_snapshot` JSON record.
The snapshot contains one twin per observed worker, sorted by worker ID. Each
twin includes its worker ID/PID, latest and previous observations, current and
previous health state, latest CPU/RSS/status fields, observation time, stale
flag, and any observed worker-failure reason. These are derived from the live
monitor stream; the Twin does not create synthetic workers or measurements.

Health transitions are deterministic:

- **HEALTHY:** process status is `running`; CPU utilization and RSS are
  available; and the observation has no per-measurement errors.
- **DEGRADED:** measurements are unavailable, the process is not reported as
  `running`, or the latest observation is stale. A missing metric by itself
  never marks a worker failed.
- **FAILED:** the OS reports a terminal `dead`/`zombie` status, or the monitor
  reports that the worker exited. Failure is sticky for that worker identity;
  a replacement PID requires a new worker identity.

The default stale threshold is three sampling intervals (with a small positive
minimum above one interval). A twin becomes stale only when the time since its
last received observation exceeds that threshold. Calling the engine's
snapshot operation refreshes freshness using a monotonic clock. The normal CLI
emits snapshots after each batch; if a worker exits during sampling, it emits
a failed twin snapshot before reporting the worker-exit error. A worker that
never becomes ready has no telemetry identity and is reported as a startup
failure rather than represented as a measured twin.

Example command:

```sh
.venv/bin/python -m twincoreos monitor \
  --workers 2 --duration 10 --sample-interval 1 --memory-mib 8
```

Each sampling cycle preserves its telemetry lines and Twin snapshot, and also
emits one `type: risk_assessment` JSON record per worker. Filter by `type` to
consume telemetry, Twin profiles, or risk assessments independently.

## Application-state checkpoints

The worker explicitly supports a small restorable application state:
`{"counter": integer, "accumulator": integer}`. Its control protocol supports
`snapshot`, `restore`, and deterministic `advance` operations. An advance by
`n` increments the counter `n` times and adds each new counter value to the
accumulator. The worker validates both fields as non-negative signed 64-bit
integers. This state is application-owned; PIDs, CPU, RSS, telemetry, Digital
Twin state, and risk assessments are never checkpointed or restored.

The monitor requests application-state snapshots at `--checkpoint-interval`
(default 5 seconds, no shorter than the telemetry sample interval) and stores
them in `--checkpoint-dir` (default `.twincoreos/checkpoints`). Checkpoints
use a strict versioned JSON schema containing a UUID, worker identity, source
PID, UTC creation time, supported application state, and SHA-256 integrity
over the canonical schema body. Loading validates the schema, supported state,
identity, and digest before restore. Writes use a same-directory temporary
file, fsync, and atomic rename. The manager uses oldest-created-first eviction
to satisfy the byte budget; a checkpoint currently being restored is
protected. If space cannot be made without evicting protected data, creation
is rejected and an event is emitted.

To exercise the worker control interface and persistent manager API, run the
checkpoint tests:

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_checkpoint_manager.py' -v
```

These are **application-level host-process state** snapshots only. They do not
capture arbitrary process memory, thread stacks, file descriptors, OS execution
context, or RTOS task context. Restoring a saved state into a fresh worker is
not restoration of the original process.

## Baseline failure-risk predictor

`src/twincoreos/risk_predictor.py` implements a replaceable `RiskPredictor`
interface with a deterministic `BaselineRiskPredictor`. There is no suitable
labeled failure dataset in this prototype, so this is a transparent heuristic,
**not a trained model and not a calibrated probability of failure**.

The default score adds configured evidence contributions and clamps the result
to `[0, 1]`: high CPU utilization adds 0.30 (elevated CPU adds 0.12 instead);
increasing CPU adds 0.20; increasing RSS adds 0.20; DEGRADED Twin health adds
0.25; stale telemetry adds 0.20; each unavailable CPU/RSS measurement adds
0.10; a recent process-state change adds 0.10; and an observed worker failure
sets score to 1.0. CPU trend uses the difference between current and previous
interval utilization in percentage points; RSS trend uses the difference
between current and previous RSS bytes. A trend is unavailable until two
observations exist. Risk category is LOW below 0.25, MEDIUM from 0.25 to below
0.60, and HIGH at 0.60 or above. The predictor reports features and reasons
that contributed to the score; missing data is represented as null, not
imputed.

Each assessment includes the worker ID/PID, score/category, observed features,
explanation, assessment and expiry timestamps, and category-transition
metadata. Its validity period defaults to three sampling intervals. These
thresholds and scores are configurable in code; they are not statistically
fitted, evaluated, or calibrated.

## Errors, process validation, and limitations

- A worker initialization error is sent over its readiness pipe and reported
  as a startup failure. A worker that exits or closes the pipe before readiness
  is reported separately from a startup timeout.
- A worker that exits during collection raises a worker-exit error and triggers
  cleanup. `psutil.AccessDenied` for an individual measurement produces a null
  field and an entry in `measurement_errors`; other collection errors are
  fatal and trigger cleanup.
- To verify live readings on macOS, use the reported PID while the monitor is
  running:

  ```sh
  ps -p <pid> -o pid=,ppid=,stat=,rss=,time=
  ```

  `ps` reports RSS in KiB on macOS; multiply by 1024 to compare with
  `memory_rss_bytes`. Process state and RSS can change or disappear between
  separate observations, so compare while the worker is alive.
- Workers are CPU-bound Python processes with a configured memory allocation;
  their behavior does not represent an RTOS task's scheduling, memory layout,
  or fault model.
- Health states summarize observed host telemetry and freshness only. They are
  not calibrated failure probabilities or checkpoint/recovery results. The
  baseline risk score is a heuristic signal only and has not been validated as
  a predictor of real-world failures.
- `psutil` process metrics depend on host-operating-system permissions and
  sampling resolution. CPU utilization can be zero for an idle/short interval.
- Host-process measurements do not demonstrate kernel-level task recovery.

## Live dashboard and HTTP API

The dashboard uses a dependency-free Python HTTP API and a React/Vite frontend.
The API runtime owns one worker pool and one telemetry loop for its entire
lifetime; page refreshes and API reads only query the current snapshot. It
refreshes the UI from the API every second. The dashboard has no mock-data or
fallback telemetry path: before a sample arrives, or when a field is
unavailable, it shows an empty or unavailable state.

The services are started and stopped using the fresh setup and
[start/stop commands above](#start-and-stop-the-dashboard). In short, launch
`twincoreos serve` in one terminal and `npm run dev` from `frontend/` in
another; press Ctrl-C in each respective terminal to stop it.

The frontend API base URL is configured by `VITE_API_URL` and defaults to
`http://127.0.0.1:8765/api`. The API service options configure worker count,
memory, sample and checkpoint intervals, storage budget/directory, startup and
shutdown timeouts, bind host, and port. The API allows browser CORS only from
the local Vite origins `http://127.0.0.1:5173` and
`http://localhost:5173`. It has no authentication and should remain bound to
loopback; do not expose it to an untrusted network.

Available JSON endpoints:

| Method and path | Content |
|---|---|
| `GET /api/dashboard` | Single bounded snapshot used by the UI, containing system, worker, telemetry, Twin, risk, checkpoint, event, and recovery data |
| `GET /api/status` | Service state, active/configured workers, sample interval |
| `GET /api/workers` | Latest worker telemetry, Twin profile, and risk per worker |
| `GET /api/telemetry` | Latest observations and bounded real observation history |
| `GET /api/twins` | Current Digital Twin snapshot |
| `GET /api/risk` | Current baseline risk assessments |
| `GET /api/embedded/risk` | Read-only embedded operational-risk assessments |
| `POST /api/embedded/events` | Validated FreeRTOS checkpoint and recovery lifecycle events |
| `GET /api/checkpoints` | Valid checkpoint inventory and actual storage usage/budget |
| `GET /api/events?limit=100` | Recent telemetry, risk, checkpoint, recovery, and service events (limit 1–500) |
| `GET /api/recovery` | Latest actual recovery outcome or `null` before any request |
| `POST /api/workers/{worker_id}/fault` | Controlled recovery; body must be `{"confirm":true}` |

Fault requests are validated against workers currently observed as active and
queued through the single monitor thread; concurrent API clients cannot start
parallel monitor loops or control the worker pool concurrently. The endpoint
returns success only after the coordinator confirms termination, restores the
checkpoint into a different PID, verifies exact state equality, and verifies
continued state progression. A failed operation returns a non-success HTTP
status and an explicit error; its real recovery-stage events remain in the
event stream.

## End-to-end dashboard demonstration

1. Follow the [fresh setup](#fresh-setup) and [start both services](#start-and-stop-the-dashboard).
2. Wait for **Backend connected**, real worker rows, and telemetry to appear at
   <http://127.0.0.1:5173/>.
3. Optionally verify the API's real status and dashboard snapshot:

   ```sh
   curl -fsS http://127.0.0.1:8765/api/status
   curl -fsS http://127.0.0.1:8765/api/dashboard
   ```

   The dashboard snapshot contains actual worker PIDs and supported
   application state. The Checkpoint storage panel shows the validated
   checkpoint inventory and current byte usage.
4. In **Controlled recovery**, select one active worker and note its PID.
   Click **Inject fault & recover**, verify the confirmation names that
   worker, then click **Confirm fault injection** once.
5. Inspect the displayed outcome and event log. A successful recovery reports
   the terminated PID, a distinct replacement PID, the restored state, and the
   continued state. Expected stage order:
   `fault_requested → worker_terminated → recovery_started →
   checkpoint_selected → restore_succeeded → recovery_completed`. Confirm the
   other worker remains present and supervised.
6. Stop Vite and the API with Ctrl-C in their respective terminals.

The dashboard’s CPU values are actual interval utilization relative to one
logical core; memory is OS-reported RSS. Risk categories and scores come from
the documented uncalibrated baseline heuristic. Checkpoint and recovery
displays concern only the worker’s explicitly supported application state,
not arbitrary process memory or kernel/RTOS context.

## Controlled fault injection and recovery

The host prototype supports a deliberately narrow fault injection: the
supervisor can terminate one of its own workers by its configured worker ID.
No arbitrary PID is accepted and no process outside the supervisor's worker
pool is targeted. After the first real telemetry sample, the CLI advances the
target worker's supported application state, writes a validated checkpoint,
terminates and waits for that worker, and marks its original Digital Twin
FAILED. It then starts a replacement with a distinct Twin identity, restores
through the worker's validated JSON control protocol, compares the resulting
state with the checkpoint, advances once more, and verifies the expected
counter and accumulator progression before emitting `recovery_completed`.

Run a two-worker end-to-end demonstration from the project root:

```sh
.venv/bin/python -m twincoreos monitor \
  --workers 2 \
  --duration 2 \
  --sample-interval 0.2 \
  --memory-mib 4 \
  --inject-fault-worker worker-1 \
  --checkpoint-dir .twincoreos/recovery-checkpoints
```

`--inject-fault-worker` accepts only an ID among the configured workers
(`worker-1`, `worker-2`, ...). This command writes JSON Lines to stdout,
including timestamped `type: recovery_event` records for the request,
confirmed termination, recovery start, checkpoint selection, restore, and
completion or failure. A `type: recovery_result` summarizes the verified
state and PIDs. The normal telemetry/Twin/risk records continue; the failed
original Twin remains under its original ID and the replacement is reported
under a new ID. A failed attempt exits nonzero and does not claim recovery.
The replacement is cleaned up on restore or verification failure; the
supervisor also terminates all remaining owned workers on exit.

This demonstrates **application-level restoration only** for the worker's
explicit `{counter, accumulator}` state. It does not restore arbitrary process
memory, execution context, threads, kernel tasks, or FreeRTOS task state. The
test suite includes live multi-process recovery tests and verifies that the
non-target worker remains alive.

## FreeRTOS-on-QEMU task demo

The isolated [embedded project](./embedded/README.md) targets the official
FreeRTOS RV32 QEMU `virt` demo, including its CLINT timer/NS16550 UART setup.
It defines two periodic FreeRTOS tasks and a versioned JSON Lines telemetry
producer with FreeRTOS task IDs, task state, tick counts, execution counts,
and measured stack high-water marks. `twincoreos embedded-bridge` launches
QEMU with its NS16550 serial console attached to a pipe, validates the JSON
Lines, and forwards each real task sample to `/api/embedded/telemetry`. The
dashboard displays those samples in a separate FreeRTOS task section and
event stream; they are not host process PIDs or host-process telemetry.

On macOS, QEMU can be installed with:

```sh
brew install qemu
```

An RV32-capable `riscv64-unknown-elf` cross compiler is also required. The
Homebrew `riscv64-elf-gcc` formula targets RV64 without the required RV32
multilib. The embedded guide documents the upstream toolchain requirements
and exact FreeRTOS source pin. Once QEMU and the cross compiler are available,
run from the project root:

```sh
export PATH="$HOME/.local/riscv/bin:$PATH"
make -C embedded deps
make -C embedded build
make -C embedded test-recovery
PYTHONPATH=src .venv/bin/python -m unittest discover \
  -s tests -p 'test_embedded_risk_checkpoint_qemu.py' -v
```

The policy-driven unittest verifies live telemetry through the bridge and backend using
the configured stack threshold, including a real QEMU checkpoint and restore.
The verified setup used GCC 14.2.0 with the `rv32imac/ilp32` multilib and
QEMU 11.1.2; the linked output is ELF32 RISC-V. To see live embedded data,
start the backend and Vite dashboard using the commands above in separate
terminals. Then, from the project root, run this in a third terminal:

```sh
export PATH="$HOME/.local/riscv/bin:$PATH"
.venv/bin/python -m twincoreos embedded-bridge
```

The bridge starts QEMU itself and forwards UART task records to the running
backend. Open <http://127.0.0.1:5173/> and inspect **Embedded RTOS**; the
execution counters and ticks update from incoming QEMU records. The same
section shows separate embedded operational-risk assessments from the
`/api/embedded/risk` policy, including its matched rule and recommendation.
This is an operational heuristic, not a trained or calibrated failure
probability. The default stack-reserve threshold is 32 words and does not
recommend a checkpoint for the observed QEMU workload. To demonstrate an
actual policy-triggered checkpoint, start the backend with the explicit
`--embedded-stack-reserve-threshold-words 200` option. The bridge polls the
read-only risk endpoint after real telemetry and writes the validated
`CHECKPOINT sensor-sampler` line to QEMU's UART input only when a fresh,
valid assessment recommends it. Firmware consumes that line at its NS16550
receive register, requests the existing task safe-point checkpoint, validates
the embedded record, and runs the existing replacement/restore sequence.
UART lifecycle events are forwarded to the backend. Duplicate requests are
latched until a fresh normal assessment clears the policy condition; unknown
or stale telemetry never triggers or re-arms a request.

This creates only an embedded RAM application-state checkpoint; it does not
alter host-process checkpoints and does not restore arbitrary task stacks,
registers, kernel or peripheral state. Stop the
bridge with **Ctrl-C**; it terminates only the QEMU process it started. To
capture UART output without forwarding it, use `make -C embedded run`.

The backend validates schema version 1 and de-duplicates each task execution
within a bridge session. `/api/embedded/tasks` returns retained samples and
`/api/dashboard` includes the same separate `embedded_tasks` data. See the
[embedded guide](./embedded/README.md) for the field definitions, toolchain
setup, and troubleshooting. The firmware also demonstrates one controlled
application-level checkpoint and restart of `sensor-sampler`, saving only its
counter and accumulator in a validated versioned RAM record. The QEMU test
checks restored state and continued progress while `control-loop` remains
active. This does not restore task stacks, registers, peripheral state, or
kernel state and is not FreeRTOS kernel-task recovery.

### Final risk-triggered demonstration and verification

Run the following in three terminals from the project root (the firmware
toolchain and QEMU must already be installed):

```sh
# Terminal 1: backend with an explicit demonstration threshold
.venv/bin/python -m twincoreos serve \
  --workers 2 --memory-mib 8 --sample-interval 1 --checkpoint-interval 5 \
  --checkpoint-budget-bytes 10485760 \
  --checkpoint-dir .twincoreos/dashboard-checkpoints \
  --embedded-stack-reserve-threshold-words 200 \
  --host 127.0.0.1 --port 8765

# Terminal 2: dashboard
cd frontend && npm run dev

# Terminal 3: firmware build if needed, then start the UART bridge
export PATH="$HOME/.local/riscv/bin:$PATH"
make -C embedded build
.venv/bin/python -m twincoreos embedded-bridge
```

Open <http://127.0.0.1:5173/> and inspect **Embedded RTOS**. Verify that
`sensor-sampler` and `control-loop` have distinct RTOS task IDs and advancing
execution counts; host workers remain in their separate PID/CPU/RSS view. To
inspect API data, use `curl -fsS http://127.0.0.1:8765/api/embedded/risk` and
`curl -fsS http://127.0.0.1:8765/api/dashboard`. After the bridge exits,
telemetry becomes stale and the policy returns `unknown` with no
recommendation; the latest checkpoint outcome remains available.

Reproducible verification commands:

```sh
export PATH="$HOME/.local/riscv/bin:$PATH"
make -C embedded build
make -C embedded test-recovery
PATH="$HOME/.local/riscv/bin:$PATH" PYTHONPATH=src \
  .venv/bin/python -m unittest discover -s tests -v
cd frontend && npm run build && npm run lint
```

On the verified setup, the full Python suite completed with 99 tests passing;
the QEMU policy integration observed 101 stack-high-water words against the
explicit 200-word threshold, then a real `checkpoint_saved` record
(`counter=1`, `accumulator=3903643392`, `integrity_hash=3567634840`), restore,
then progressed from `counter=1` to `counter=2` with
`accumulator=2047425920`, while `control-loop` reached execution count 2 and
continued through count 10. The observed UART lifecycle was
`checkpoint_requested → checkpoint_saved → task_restart_started →
task_restarted → restore_succeeded → task_progress → recovery_completed`.
Firmware build, direct QEMU recovery test, frontend build, and frontend lint
also passed. These values document an actual run, not seeded runtime data;
subsequent runs may differ.
Focused tests also cover below-threshold and stale/invalid telemetry, duplicate
request latching/re-arming, and rejection of mismatched checkpoint hashes.
There is no deterministic firmware fault-injection hook for task creation or
restore-resource failures, so those failure branches are not claimed as
QEMU-tested; the firmware reports them as failures and does not emit successful
recovery completion.
