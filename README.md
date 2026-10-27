# TwinCoreOS

TwinCoreOS is an OS/RTOS-focused fault-tolerance prototype. Its dashboard
shows a **FreeRTOS-on-QEMU embedded task risk assessment** when UART telemetry
is connected, while a separate host-monitoring section supervises real Python
worker subprocesses. Host monitoring samples OS measurements through `psutil`,
maintains per-worker Digital Twins, and uses a trained host-telemetry anomaly
detector when its model artifact is available (otherwise a baseline
assessment). A separate embedded Isolation Forest can be trained from
explicitly curated FreeRTOS UART observations and runs in shadow mode only;
it never replaces the embedded threshold policy or dispatches checkpoints.
The system also demonstrates validated host application-state
checkpoints and controlled worker recovery through a local API and React
dashboard.

It is not a kernel-integrated RTOS monitor, a calibrated failure predictor, or
arbitrary process-memory restoration. The optional Isolation Forest detects
statistical deviations in host telemetry; it does not predict worker failure.
The separate RV32 FreeRTOS-on-QEMU prototype under `embedded/` sends actual
task telemetry over emulated UART through the bridge and backend embedded-risk
policy into the main Risk Assessment panel. Embedded task measurements and
risk remain separate from host-process identities, metrics, and the host
Isolation Forest. Embedded shadow anomaly scores are likewise not calibrated
failure probabilities and are not used as embedded risk scores.

## Prerequisites and project structure

Requirements for the host prototype:

- Python 3.10 or newer.
- Node.js and npm versions supported by the Vite version in
  `frontend/package-lock.json`.
- A local loopback interface with ports 8765 (API) and 5173 (Vite) available,
  or matching configuration changes to the API command, Vite server, and
  `VITE_API_URL`.

No Docker, C compiler, or emulator is required for the host-process
demonstration. scikit-learn is installed with the Python package for host
telemetry anomaly detection.

```text
src/twincoreos/       Worker, monitor, Digital Twin, risk, checkpoints, recovery, API
tests/                Unit tests and process/API integration tests
frontend/src/         React dashboard
frontend/package.json Frontend commands and dependencies
frontend/package-lock.json Locked frontend dependencies for reproducible npm ci
frontend/.env.example Frontend API URL template
pyproject.toml        Python package metadata and runtime dependencies
```

## Fresh setup

From the project root, create an isolated Python environment and install the
package and its runtime dependencies (`psutil` and `scikit-learn`):

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
workers. Startup logs show the actual API listening address and whether the
trained Isolation Forest or baseline fallback is active. To stop the backend,
press **Ctrl-C in Terminal 1**. The service closes the HTTP server and shuts
down and reaps its supervised workers.

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

## Risk predictors

`src/twincoreos/risk_predictor.py` implements a replaceable `RiskPredictor`
interface with a deterministic `BaselineRiskPredictor`. It remains available
for comparison and is the explicit fallback if an ML artifact is missing,
invalid, or inference fails. It is a transparent heuristic, **not a calibrated
probability of failure**.

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
metadata. Its validity period defaults to three sampling intervals. The
baseline predictor remains useful as a comparison point; its thresholds and
scores are not statistically fitted, evaluated, or calibrated.

### Train the host-telemetry Isolation Forest

The repository contains **no suitable labeled historical worker-failure
dataset**. The initial ML model is therefore an unsupervised scikit-learn
Isolation Forest, not a supervised failure classifier. It identifies unusual
host-worker observations relative to the observations it was fitted on. It
does not estimate failure probability, establish a failure horizon, identify a
cause, or guarantee that an inlier worker is healthy.

Collect real host-worker telemetry using the existing monitor. Its stdout is
JSON Lines containing telemetry and other event types; the trainer selects
only records with `"type": "telemetry"`. This preserves host-process
measurements and does not mix in FreeRTOS task telemetry.

```sh
.venv/bin/python -m twincoreos monitor \
  --workers 2 --duration 120 --sample-interval 1 --memory-mib 8 \
  > .twincoreos/host-telemetry.jsonl
```

The training dataset is that collected JSONL stream: each accepted record is
one actual host-worker observation with fields including `task_id`, `pid`,
`process_created_at_utc`, `captured_at_utc`,
`cpu_utilization_percent_one_core`, `memory_rss_bytes`, `cpu_interval_seconds`,
`process_status`, and `measurement_errors`. Non-telemetry events are ignored;
malformed JSON or invalid telemetry fails training with a line-specific error.
The trainer groups and chronologically orders observations by worker process
identity to derive CPU/RSS trends and sample gaps. It requires at least 20
telemetry observations. This minimum is an input check, **not evidence of
adequate coverage or predictive accuracy**. Do not add synthetic rows to meet
the requirement.

Train and persist a reproducible model artifact:

```sh
.venv/bin/python -m twincoreos.train_risk_model \
  --input .twincoreos/host-telemetry.jsonl \
  --output .twincoreos/models/host-isolation-forest.joblib \
  --contamination 0.05 --random-state 42
```

`contamination` is the expected outlier fraction used to set the Isolation
Forest decision boundary; adjust it for the collection workload. Training fits
a median imputer, standard scaler, and Isolation Forest and writes the
preprocessing and model together. There is no per-request or per-sample
retraining. Training reports the model type and number of observations; it
does not report failure-prediction accuracy because there are no failure
labels.

Start the backend with that artifact (both `monitor` and `serve` accept the
same options):

```sh
.venv/bin/python -m twincoreos serve \
  --workers 2 --sample-interval 1 \
  --risk-model .twincoreos/models/host-isolation-forest.joblib
```

Check `GET /api/risk` or the existing `GET /api/dashboard` response. ML-backed
assessments preserve the existing `RiskAssessment` and `to_dict()` fields. The
`observed_features` object includes `model_type: "isolation_forest"`,
`anomaly_score`, and `anomaly_status` (`"anomalous"` or `"inlier"`), in
addition to the measured feature summary. `anomaly_score` is the signed
distance relative to the fitted decision boundary (`> 0` is anomalous); it is
not a failure probability.

For compatibility with the existing API and dashboard, `risk_score` and
`risk_category` remain present. The default mapping explicitly assigns score
`0.0` to inliers (LOW) and `0.60` to anomalies (HIGH), using the existing
category cutoffs. Configure that compatibility mapping at launch with
`--normal-risk-score` and `--anomaly-risk-score` (each in `[0, 1]`); for
example, an anomaly score of `0.30` maps to MEDIUM with the current thresholds.
The score is only a category-compatibility index and **must not be interpreted
as probability or calibrated risk**. The dashboard labels this value as an
index rather than a percentage. A newly observed worker is shown as
`WARMING_UP` with no score until a second telemetry sample is available for
per-worker trend features; this does not change the separate process-health
status. Failed worker records remain associated with their original worker ID
after a replacement starts, while the replacement's current health and risk
are assessed independently. The API response retains its existing fields.

The fixed model features are one-core CPU utilization, RSS, CPU and RSS
observation-to-observation trends, sample-to-sample telemetry gap, Digital
Twin health, Twin staleness, CPU/RSS missingness, measurement-error count, and
process-status change. Health states are encoded separately, and unavailable
numeric values are median-imputed from the real training observations (with
explicit missing indicators). Feature order is versioned and validated when
loading, and the artifact's exact scikit-learn version must match the runtime;
otherwise the service logs a warning and falls back to the baseline. Stale
observations and missing values remain visible in the returned feature summary.

When the default artifact is absent (the checked-in project does not include a
trained model), startup logs a warning and uses `BaselineRiskPredictor`. A
corrupt/incompatible artifact or runtime inference failure also logs the
fallback and uses the baseline without taking down monitoring. Confirmed
worker failure always bypasses ML and receives the deterministic baseline
failure assessment. ML outputs alone do not initiate checkpointing or
recovery; existing checkpoint validation, recovery controls, and embedded
FreeRTOS behavior remain unchanged.

The model is persisted with joblib. Only load model artifacts that you trust;
joblib artifacts use Python object serialization and loading an untrusted
artifact can execute code. Retrain after changing the feature schema or scikit-
learn versions and verify the logged `model_type`/fallback status before
interpreting assessments.

To compare against the retained heuristic, run the same telemetry workload
once with the trained artifact and once with a nonexistent model path (for
example, `--risk-model /tmp/no-such-model.joblib`). Compare `risk_category`,
the ML `anomaly_status`/`anomaly_score`, and the baseline's additive
`observed_features`/explanation. This is a qualitative comparison only, not a
failure-prediction benchmark.

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
backend. Open <http://127.0.0.1:5173/> and inspect the main **Risk Assessment**
panel for both FreeRTOS tasks; host workers remain in their separate host
monitoring section. Task state, measured stack reserve, configured threshold,
telemetry age, policy category/index, explanation, and checkpoint status all
come from the backend embedded-risk policy. Its discrete policy index is not a
trained or calibrated failure probability. The default threshold is 32 words;
the fault demo below uses 96 words so the firmware's measured reserve drop
crosses the configured rule. The UART bridge can request the bounded
`sensor-sampler` stack-pressure fault only after both tasks have fresh LOW-risk
telemetry. The bridge then forwards actual UART telemetry and lifecycle events;
the backend recommends checkpointing only for `sensor-sampler`, and the
firmware performs its supported safe-point application-state checkpoint and
restart/restore. `control-loop` checkpointing is unsupported and is not
requested. Duplicate checkpoint requests are latched until a fresh normal
assessment clears the policy condition; unknown or stale telemetry never
triggers or re-arms a request.

This creates only an embedded RAM application-state checkpoint; it does not
alter host-process checkpoints and does not restore arbitrary task stacks,
registers, kernel or peripheral state. Stop the
bridge with **Ctrl-C**; it terminates only the QEMU process it started. To
capture UART output without forwarding it, use `make -C embedded run`.

The backend validates schema version 1 and de-duplicates each task execution
within a bridge session. `/api/embedded/tasks` returns retained samples and
`/api/dashboard` includes the same `embedded_tasks` data and policy
assessments. See the [embedded guide](./embedded/README.md) for the field
definitions, toolchain setup, and troubleshooting. The supported
`sensor-sampler` checkpoint saves only its counter and accumulator in a
validated, versioned RAM record. The QEMU test checks restored state and
continued progress. This does not restore task stacks, registers, peripheral
state, or kernel state and is not FreeRTOS kernel-task recovery.

The dashboard's current-session checkpoint slot is populated only by a
firmware-confirmed `checkpoint_saved` event and is cleared when telemetry
identifies a new QEMU bridge session. The recovery history is separate: it
retains up to 20 run records in the running backend process, including saved,
restored, and post-restore application-state evidence. Historical records do
not mean that the volatile checkpoint still exists in QEMU RAM. The embedded
storage panel reports the actual firmware record size when a current-session
save is confirmed; one RAM slot is available and no byte budget is configured.
Risk-category transitions are also retained with task, time, measured reserve,
threshold, and policy index, while the live task card continues to reflect the
latest fresh telemetry. LOW/0 and ELEVATED/100 are discrete policy states, not
failure probabilities.

### Live embedded fault, risk, and recovery demonstration

The demo requires the RISC-V GCC toolchain supporting `rv32imac/ilp32`, QEMU,
the project's `.venv`, and installed frontend dependencies. Run these in
three terminals from the repository root. The backend creates an isolated
temporary checkpoint directory so the demo does not write into existing
checkpoint data.

**Terminal 1 — backend and host monitoring:**

```sh
DEMO_CHECKPOINT_DIR=$(mktemp -d /tmp/twincoreos-embedded-demo.XXXXXX)
printf 'Demo checkpoints: %s\n' "$DEMO_CHECKPOINT_DIR"
PYTHONPATH=src .venv/bin/python -m twincoreos serve \
  --workers 2 \
  --memory-mib 8 \
  --sample-interval 1 \
  --checkpoint-interval 5 \
  --checkpoint-budget-bytes 10485760 \
  --checkpoint-dir "$DEMO_CHECKPOINT_DIR" \
  --embedded-stack-reserve-threshold-words 96 \
  --host 127.0.0.1 \
  --port 8765
```

Keep this terminal open. Confirm it logs the actual listening address and
whether the trained host Isolation Forest or baseline host predictor is
active. That host predictor is not used for embedded task risk.
The serve CLI default for this setting is 32 words; this demo explicitly sets
96. The active value comes from this CLI option, not a frontend default; 200
words was an earlier requested demonstration value, not the current command's
configuration. The 96-word threshold makes the measured bounded fault a
policy-triggering signal while allowing baseline task telemetry to be checked
against the same explicit rule. In a recent live two-cycle QEMU test, both
tasks reported 246 words before injection and the injected sensor task reported
95 words; the policy therefore crossed the 96-word boundary using measured
telemetry. These are observed readings, not fixed guarantees. The backend
returns the configured value in each assessment; it is never hardcoded in the
dashboard.

**Terminal 2 — React/Vite dashboard:**

```sh
cd frontend
npm run dev
```

Open <http://127.0.0.1:5173/>. The main **Risk Assessment** panel renders
`sensor-sampler` and `control-loop` assessments from the backend policy;
host-worker PID/CPU/RSS risk remains separate.

**Terminal 3 — build firmware and run the live UART fault demo:**

```sh
export PATH="$HOME/.local/riscv/bin:$PATH"
make -C embedded build
PYTHONPATH=src .venv/bin/python -m twincoreos embedded-bridge
```

Open the dashboard and use **Embedded recovery demonstration → Inject fault
& verify application-state recovery**. The API accepts this request only when
both tasks have fresh, running LOW assessments. The bridge polls the
embedded-only command endpoint and sends the supported
`INJECT_STACK_PRESSURE sensor-sampler <run-id>` command through QEMU UART.
Each request gets a unique run ID, and the firmware echoes it in task telemetry
and lifecycle acknowledgements. The dashboard retains separate timestamped
evidence for each run. The host-worker fault/recovery API remains separate and
is not invoked by this embedded control.

To run the same workflow without the dashboard button, issue the request once
and wait for verified completion plus fresh LOW telemetry from both tasks
before issuing it a second time:

```sh
curl -fsS -X POST http://127.0.0.1:8765/api/embedded/fault-injection \
  -H 'Content-Type: application/json' \
  -d '{"task_id":"sensor-sampler","confirm":true}'
curl -fsS http://127.0.0.1:8765/api/embedded/fault-injection
curl -fsS http://127.0.0.1:8765/api/embedded/risk
```

The firmware uses a bounded 144-word local buffer in `sensor-sampler` and
reports the actual FreeRTOS stack high-water measurement while that frame is
live. The measured reserve drops to or below the configured 96-word threshold,
so the backend—not React—updates the task to ELEVATED and recommends a
checkpoint. Both normal task stacks and replacement task stacks are sized to
three times `configMINIMAL_STACK_SIZE`; this provides sufficient measured
reserve for normal operation and leaves the bounded fault observable without
changing the configured risk threshold. The bridge sends the
backend-authorized checkpoint command; the firmware reports request, save,
restore start, restore verification, and restored-state progress as separate
UART events. It saves only `sensor-sampler` application state (counter and
accumulator) in a validated RAM checkpoint. A later injection is enabled only
after the backend confirms recovery completion and receives newer, same-run
telemetry showing progress with both tasks back in fresh LOW state. Click the
control a second time only after it becomes enabled; the UI never reuses an
older run or checkpoint. `control-loop` continues reporting independently and
never receives a checkpoint request. This verifies application-state
restoration and progress, not whole-task/FreeRTOS context recovery.

The supported CLI-only alternative to the dashboard button is
`.venv/bin/python -m twincoreos embedded-bridge --inject-stack-pressure sensor-sampler`;
it injects automatically after the bridge sees both tasks in fresh LOW state.
The bridge continues polling for dashboard requests after this one-shot
injection and recovery.

To confirm actual task and policy data while the bridge is running:

```sh
curl -fsS http://127.0.0.1:8765/api/embedded/tasks
curl -fsS http://127.0.0.1:8765/api/embedded/risk
curl -fsS http://127.0.0.1:8765/api/embedded/fault-injection
curl -fsS http://127.0.0.1:8765/api/dashboard
```

The supported fault is a bounded stack-pressure event, not arbitrary memory
corruption or a physical sensor fault. `sensor-sampler` is the only task with
checkpoint/restart support; `control-loop` correctly reports checkpointing as
unsupported. The discrete risk index is a policy indicator, not a failure
probability. When the bridge is stopped, telemetry eventually becomes stale
and the policy reports UNKNOWN rather than healthy/zero-risk.

Stop the bridge/QEMU, backend, and Vite server with **Ctrl-C** in their
respective terminals. For a standalone serial view without backend forwarding, use `make -C embedded
run`; exit the QEMU monitor with **Ctrl-A**, then **X**. Do not run that
standalone mode concurrently with the bridge.

### Verification

Run the actual firmware build, direct recovery and full policy-driven QEMU
integration, Python suite, and frontend checks:

```sh
export PATH="$HOME/.local/riscv/bin:$PATH"
make -C embedded build
make -C embedded test-recovery
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
cd frontend
npm test
npm run lint
npm run build
```

The live QEMU integration test executes two sequential dashboard/API-triggered
runs with distinct run IDs. It verifies fresh baseline LOW assessments,
run-correlated UART fault acknowledgements and below-threshold measurements,
intermediate ELEVATED recommendations, checkpoint integrity, saved/restored
application-state equality, counter progress beyond restored state, and fresh
post-recovery LOW telemetry before permitting the second run. `control-loop`
telemetry remains independently assessed and checkpointing stays unsupported.
The test uses live QEMU telemetry; values can vary between runs.

### Embedded ML shadow pipeline (Stage 1)

The embedded-specific Isolation Forest is an optional, unsupervised shadow
detector. It uses only schema-validated `sensor-sampler` and `control-loop`
UART telemetry, with per-task temporal features. It does not consume host
worker PID, CPU, RSS, host risk, or host failure data. The existing embedded
threshold policy remains the sole source of operational risk and checkpoint
recommendations; `GET /api/embedded/ml` and `/api/dashboard` expose separate
ML assessments and shadow-only proposals. The API reports that checkpoint
commands are disabled in shadow mode.

The dashboard displays each task's model status, anomaly/inlier result, raw
anomaly score, model version, and observation timestamp separately from its
threshold-policy category and index. It accepts a prediction only when its
task, bridge session, model version, and timestamp match the latest fresh
telemetry; absent, stale, disconnected, or mismatched inference is shown as
unavailable. The raw Isolation Forest score is not a calibrated failure
probability, and shadow output cannot issue or suppress threshold-based
checkpoint commands.

There is no embedded dataset or trained embedded model shipped with the
project. Until a compatible artifact is trained, the backend logs why ML
inference is unavailable and continues using the unchanged threshold policy.
The host Isolation Forest artifact is not compatible and must not be used for
embedded inference. Model artifacts use joblib serialization and must come
from a trusted local training run; do not load an untrusted artifact.

Start the backend with the intended telemetry and model locations. The data
directory is separate from host telemetry and checkpoint storage:

```sh
PYTHONPATH=src .venv/bin/python -m twincoreos serve \
  --workers 2 \
  --memory-mib 8 \
  --sample-interval 1 \
  --checkpoint-interval 5 \
  --checkpoint-budget-bytes 10485760 \
  --checkpoint-dir .twincoreos/dashboard-checkpoints \
  --embedded-stack-reserve-threshold-words 96 \
  --embedded-data-dir .twincoreos/embedded-data \
  --embedded-ml-model .twincoreos/models/embedded-isolation-forest.joblib \
  --host 127.0.0.1 \
  --port 8765
```

In another terminal, build/start the same firmware and bridge used by the
embedded demo, without fault injection while collecting baseline samples:

```sh
export PATH="$HOME/.local/riscv/bin:$PATH"
make -C embedded build
PYTHONPATH=src .venv/bin/python -m twincoreos embedded-bridge
```

The bridge prints its `bridge_session_id` at startup. Collect a fresh,
fault-free running baseline for both tasks, then stop only the bridge with
Ctrl-C. Repeat the bridge session to obtain an independent source group. While
each session is live, check that `/api/embedded/tasks` contains fresh samples
for both tasks and that `/api/embedded/risk` reports the backend policy
assessment. The shadow collector appends accepted non-duplicate records to
`.twincoreos/embedded-data/live-observations.jsonl`; it never inserts
test-generated records into that live journal.

Inspect the journal, then explicitly curate each verified normal session
(replace the two example session IDs with the values printed by the bridge):

```sh
PYTHONPATH=src .venv/bin/python -m twincoreos.embedded_ml inspect \
  --input .twincoreos/embedded-data/live-observations.jsonl

BASELINE_SESSION_1='paste-first-bridge-session-id'
BASELINE_SESSION_2='paste-second-bridge-session-id'
PYTHONPATH=src .venv/bin/python -m twincoreos.embedded_ml curate-baseline \
  --input .twincoreos/embedded-data/live-observations.jsonl \
  --output .twincoreos/embedded-data/baseline-session-1.jsonl \
  --bridge-session-id "$BASELINE_SESSION_1" \
  --threshold-words 96 \
  --confirm-normal-baseline
PYTHONPATH=src .venv/bin/python -m twincoreos.embedded_ml curate-baseline \
  --input .twincoreos/embedded-data/live-observations.jsonl \
  --output .twincoreos/embedded-data/baseline-session-2.jsonl \
  --bridge-session-id "$BASELINE_SESSION_2" \
  --threshold-words 96 \
  --confirm-normal-baseline
```

Training requires at least 30 explicitly curated, fresh baseline records
covering both tasks and at least two independent QEMU bridge sessions. If these
checks fail, collect more data; do not lower the guard or add simulated data
to the training inputs. The command creates a separate embedded model and
refuses to overwrite it unless `--overwrite` is explicitly supplied:

```sh
PYTHONPATH=src .venv/bin/python -m twincoreos.embedded_ml train \
  --input .twincoreos/embedded-data/baseline-session-1.jsonl \
  --input .twincoreos/embedded-data/baseline-session-2.jsonl \
  --output .twincoreos/models/embedded-isolation-forest.joblib \
  --contamination 0.05 \
  --random-state 42
```

For a shadow evaluation, collect two further independent QEMU sessions not
used for training (one validation source and a different held-out test
source). After each session has fresh telemetry from both tasks, stop its
bridge and generate explicitly marked operating-condition scenarios from
that session only:

```sh
VALIDATION_SESSION='paste-validation-bridge-session-id'
TEST_SESSION='paste-held-out-bridge-session-id'
PYTHONPATH=src .venv/bin/python -m twincoreos.embedded_ml generate-scenarios \
  --input .twincoreos/embedded-data/live-observations.jsonl \
  --output .twincoreos/embedded-data/validation-scenarios.jsonl \
  --bridge-session-id "$VALIDATION_SESSION" \
  --threshold-words 96
PYTHONPATH=src .venv/bin/python -m twincoreos.embedded_ml generate-scenarios \
  --input .twincoreos/embedded-data/live-observations.jsonl \
  --output .twincoreos/embedded-data/test-scenarios.jsonl \
  --bridge-session-id "$TEST_SESSION" \
  --threshold-words 96
PYTHONPATH=src .venv/bin/python -m twincoreos.embedded_ml evaluate \
  --model .twincoreos/models/embedded-isolation-forest.joblib \
  --validation .twincoreos/embedded-data/validation-scenarios.jsonl \
  --test .twincoreos/embedded-data/test-scenarios.jsonl \
  --threshold-words 96
```

Evaluation rejects source-group overlap. Its reference conditions describe
the explicitly simulated threshold, progress, and recovery operating
conditions; they are not observed task failures, independent failure labels,
or proof of real-world predictive performance. The output compares those
conditions with both the existing threshold rule and the ML anomaly detector,
and reports the limitations. After a compatible artifact is available, restart
the backend with `--embedded-ml-model` pointing to it. `/api/embedded/ml`
returns the loaded model version, current shadow assessments, differences
from the threshold policy, journal status, and the explicit
`checkpoint_command_issued: false` flag. No adaptive checkpointing or policy
change is part of this stage.
