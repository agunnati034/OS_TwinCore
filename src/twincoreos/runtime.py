"""Long-lived owner for worker monitoring and dashboard state."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import json
from pathlib import Path
import queue
import threading
import time
import uuid
from typing import Any

from twincoreos.checkpoint_manager import CheckpointError, CheckpointManager
from twincoreos.digital_twin import DigitalTwinEngine
from twincoreos.embedded_risk import EMBEDDED_TASK_IDS, EmbeddedRiskPolicy
from twincoreos.embedded_telemetry import (
    EmbeddedTelemetryError,
    validate_task_event,
    validate_task_telemetry,
)
from twincoreos.embedded_ml import (
    DEFAULT_EMBEDDED_DATA_DIR,
    DEFAULT_EMBEDDED_MODEL_PATH,
    EmbeddedMLShadowMode,
)
from twincoreos.monitor import WorkerPool
from twincoreos.recovery import RecoveryCoordinator, RecoveryError
from twincoreos.ml_risk_predictor import (
    DEFAULT_MODEL_PATH,
    create_risk_predictor,
)

EMBEDDED_RECOVERY_DEMO_MIN_THRESHOLD_WORDS = 96


class RuntimeUnavailableError(RuntimeError):
    """The monitor runtime is not ready to accept an operation."""


class UnknownWorkerError(ValueError):
    """A requested worker ID is not currently active."""


class EmbeddedTelemetryConflictError(ValueError):
    """A repeated telemetry identity contains different measurements."""


class EmbeddedTelemetryOutOfOrderError(ValueError):
    """An embedded task sample regressed within one bridge session."""

class EmbeddedStaleRunEvidenceError(ValueError):
    """Embedded telemetry or acknowledgements belong to an earlier run."""


class EmbeddedFaultRequestError(ValueError):
    """An embedded fault request is invalid or cannot be safely dispatched."""


class RecoveryRequestError(RuntimeError):
    """A recovery request failed, with its emitted backend event sequence."""

    def __init__(self, message: str, events: list[dict[str, Any]]) -> None:
        self.events = events
        super().__init__(message)


class _FaultRequest:
    def __init__(self, worker_id: str) -> None:
        self.worker_id = worker_id
        self.completed = threading.Event()
        self.result: dict[str, Any] | None = None
        self.error: Exception | None = None
        self.events: list[dict[str, Any]] = []


class MonitorRuntime:
    """Own exactly one worker pool and update a thread-safe dashboard snapshot."""

    def __init__(
        self,
        workers: int = 2,
        memory_mib: int = 8,
        sample_interval: float = 1.0,
        checkpoint_interval: float = 5.0,
        checkpoint_budget_bytes: int = 10 * 1024 * 1024,
        checkpoint_dir: str | Path = ".twincoreos/checkpoints",
        embedded_stack_reserve_threshold_words: int = 32,
        embedded_telemetry_stale_after_seconds: float = 3.0,
        startup_timeout: float = 10.0,
        shutdown_timeout: float = 5.0,
        risk_model_path: str | Path = DEFAULT_MODEL_PATH,
        normal_risk_score: float = 0.0,
        anomaly_risk_score: float = 0.60,
        embedded_data_dir: str | Path = DEFAULT_EMBEDDED_DATA_DIR,
        embedded_ml_model_path: str | Path = DEFAULT_EMBEDDED_MODEL_PATH,
    ) -> None:
        if workers < 1 or memory_mib < 1:
            raise ValueError("workers and memory_mib must be positive")
        if sample_interval <= 0 or checkpoint_interval < sample_interval:
            raise ValueError(
                "checkpoint_interval must be at least the positive sample_interval"
            )
        self.workers = workers
        self.memory_mib = memory_mib
        self.sample_interval = sample_interval
        self.checkpoint_interval = checkpoint_interval
        self.checkpoint_budget_bytes = checkpoint_budget_bytes
        self.checkpoint_dir = Path(checkpoint_dir)
        self.startup_timeout = startup_timeout
        self.shutdown_timeout = shutdown_timeout
        stale_after = max(3.0 * sample_interval, sample_interval + 0.001)
        self._twins = DigitalTwinEngine(stale_after_seconds=stale_after)
        self._predictor = create_risk_predictor(
            risk_model_path,
            assessment_validity_seconds=stale_after,
            normal_risk_score=normal_risk_score,
            anomaly_risk_score=anomaly_risk_score,
        )
        self._checkpoints = CheckpointManager(
            self.checkpoint_dir, checkpoint_budget_bytes
        )
        self._guard = threading.RLock()
        self._ready = threading.Event()
        self._stopping = threading.Event()
        self._fault_requests: queue.Queue[_FaultRequest] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._status = "starting"
        self._error: str | None = None
        self._started_at_utc: str | None = None
        self._latest_telemetry: dict[str, dict[str, Any]] = {}
        self._risks: dict[str, dict[str, Any]] = {}
        self._history: deque[dict[str, Any]] = deque(maxlen=500)
        self._events: deque[dict[str, Any]] = deque(maxlen=500)
        self._embedded_latest: dict[tuple[str, str], dict[str, Any]] = {}
        self._embedded_bridge_session_id: str | None = None
        self._embedded_risk_categories: dict[str, str] = {}
        self._embedded_history: deque[dict[str, Any]] = deque(maxlen=500)
        self._embedded_risk_events: deque[dict[str, Any]] = deque(maxlen=100)
        self._embedded_seen: dict[tuple[str, str, int], dict[str, Any]] = {}
        self._embedded_last_execution: dict[tuple[str, str], int] = {}
        self._embedded_checkpoint_status: dict[str, dict[str, Any]] = {}
        self._embedded_checkpoint_storage: dict[str, Any] | None = None
        self._embedded_fault_injection: dict[str, Any] | None = None
        self._embedded_fault_runs: deque[dict[str, Any]] = deque(maxlen=20)
        self._embedded_risk_policy = EmbeddedRiskPolicy(
            minimum_stack_high_water_words=embedded_stack_reserve_threshold_words,
            stale_after_seconds=embedded_telemetry_stale_after_seconds,
        )
        self._embedded_ml_shadow = EmbeddedMLShadowMode(
            model_path=embedded_ml_model_path,
            data_dir=embedded_data_dir,
            stale_after_seconds=embedded_telemetry_stale_after_seconds,
        )
        self._last_recovery: dict[str, Any] | None = None
        self._last_checkpointed_state: dict[str, dict[str, int]] = {}
        self._pool: WorkerPool | None = None
        latest_checkpoints: dict[str, dict[str, Any]] = {}
        for checkpoint in self._checkpoints.list_checkpoints():
            previous = latest_checkpoints.get(checkpoint["worker_id"])
            if previous is None or (
                checkpoint["created_at_utc"],
                checkpoint["checkpoint_id"],
            ) > (previous["created_at_utc"], previous["checkpoint_id"]):
                latest_checkpoints[checkpoint["worker_id"]] = checkpoint
        self._last_checkpointed_state = {
            worker_id: dict(checkpoint["application_state"])
            for worker_id, checkpoint in latest_checkpoints.items()
        }
        self._record_checkpoint_events()

    def start(self) -> None:
        if self._thread is not None:
            if self._thread.is_alive() and self._status == "running":
                return
            raise RuntimeUnavailableError("monitor runtime has already been started")
        self._thread = threading.Thread(
            target=self._run, name="twincoreos-monitor", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(self.startup_timeout + 2.0):
            self.stop()
            raise RuntimeUnavailableError("monitor startup did not complete in time")
        with self._guard:
            if self._status != "running":
                raise RuntimeUnavailableError(
                    self._error or "monitor runtime failed to start"
                )

    def stop(self) -> None:
        self._stopping.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(
                timeout=self.startup_timeout + self.shutdown_timeout + 7.0
            )
            if self._thread.is_alive():
                raise RuntimeUnavailableError(
                    "monitor thread did not stop within its shutdown deadline"
                )
        with self._guard:
            if self._status not in {"failed", "stopped"}:
                self._status = "stopped"
                self._record_event(
                    {
                        "type": "service_event",
                        "event": "monitor_stopped",
                        "occurred_at_utc": self._now(),
                    }
                )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _copy(value: Any) -> Any:
        return json.loads(json.dumps(value, allow_nan=False))

    def _record_event(self, event: dict[str, Any]) -> None:
        with self._guard:
            self._events.append(self._copy(event))

    def _record_checkpoint_events(self) -> None:
        for event in self._checkpoints.drain_events():
            self._record_event(event)

    def _run(self) -> None:
        try:
            with WorkerPool(
                self.workers,
                self.memory_mib,
                startup_timeout=self.startup_timeout,
                shutdown_timeout=self.shutdown_timeout,
            ) as pool:
                with self._guard:
                    self._pool = pool
                    self._status = "running"
                    self._started_at_utc = self._now()
                    self._record_event(
                        {
                            "type": "service_event",
                            "event": "monitor_started",
                            "occurred_at_utc": self._started_at_utc,
                            "worker_count": self.workers,
                        }
                    )
                self._ready.set()
                monitor_started = time.monotonic()
                next_checkpoint = monitor_started + self.checkpoint_interval
                while not self._stopping.wait(self.sample_interval):
                    try:
                        self._handle_fault_requests(pool)
                        samples = pool.sample(monitor_started)
                        with self._guard:
                            for sample in samples:
                                self._latest_telemetry[sample["task_id"]] = sample
                                self._history.append(self._copy(sample))
                                self._record_event(sample)
                                twin = self._twins.update(sample)
                                assessment = self._predictor.assess(twin).to_dict()
                                self._risks[twin.worker_id] = assessment
                                self._record_event(assessment)
                    except Exception as error:
                        with self._guard:
                            self._error = str(error)
                            self._record_event(
                                {
                                    "type": "service_event",
                                    "event": "monitor_error",
                                    "occurred_at_utc": self._now(),
                                    "error": str(error),
                                }
                            )
                        raise
                    if time.monotonic() >= next_checkpoint:
                        self._create_periodic_checkpoints(pool)
                        next_checkpoint = time.monotonic() + self.checkpoint_interval
                self._handle_fault_requests(pool, reject=True)
        except Exception as error:
            with self._guard:
                self._status = "failed"
                self._error = str(error)
                self._record_event(
                    {
                        "type": "service_event",
                        "event": "monitor_failed",
                        "occurred_at_utc": self._now(),
                        "error": str(error),
                    }
                )
        finally:
            with self._guard:
                self._pool = None
            self._ready.set()

    def _create_periodic_checkpoints(self, pool: WorkerPool) -> None:
        for worker_id in sorted(pool.active_worker_ids()):
            try:
                self._checkpoint_worker(pool, worker_id)
            except CheckpointError:
                self._record_checkpoint_events()
            except Exception as error:
                self._record_event(
                    {
                        "type": "checkpoint_operation_error",
                        "event": "periodic_checkpoint_failed",
                        "worker_id": worker_id,
                        "occurred_at_utc": self._now(),
                        "error": str(error),
                    }
                )
            self._record_checkpoint_events()
        with self._guard:
            self._record_event(
                {
                    "type": "checkpoint_status",
                    "occurred_at_utc": self._now(),
                    "storage_usage_bytes": self._checkpoints.storage_usage_bytes(),
                    "storage_budget_bytes": self.checkpoint_budget_bytes,
                }
            )

    def _checkpoint_worker(self, pool: WorkerPool, worker_id: str) -> None:
        pid, state = pool.application_state(worker_id)
        if self._last_checkpointed_state.get(worker_id) == state:
            return
        self._checkpoints.create_checkpoint(worker_id, pid, state)
        self._last_checkpointed_state[worker_id] = dict(state)

    def _handle_fault_requests(
        self, pool: WorkerPool, reject: bool = False
    ) -> None:
        while True:
            try:
                request = self._fault_requests.get_nowait()
            except queue.Empty:
                return
            if reject:
                request.error = RuntimeUnavailableError("monitor is stopping")
            else:
                try:
                    self._checkpoint_worker(pool, request.worker_id)
                    self._record_checkpoint_events()
                    coordinator = RecoveryCoordinator(
                        pool, self._twins, self._checkpoints
                    )
                    try:
                        with self._guard:
                            result = coordinator.recover(request.worker_id)
                            for twin in self._twins.states():
                                assessment = self._predictor.assess(twin).to_dict()
                                self._risks[twin.worker_id] = assessment
                                self._record_event(assessment)
                        request.result = result
                        with self._guard:
                            self._last_recovery = {
                                **result,
                                "status": "completed",
                                "completed_at_utc": self._now(),
                            }
                    except RecoveryError as error:
                        request.error = error
                        with self._guard:
                            for twin in self._twins.states():
                                assessment = self._predictor.assess(twin).to_dict()
                                self._risks[twin.worker_id] = assessment
                                self._record_event(assessment)
                            self._last_recovery = {
                                "worker_id": request.worker_id,
                                "status": "failed",
                                "error": str(error),
                                "completed_at_utc": self._now(),
                            }
                    finally:
                        stage_events = coordinator.drain_events()
                        request.events.extend(stage_events)
                        for event in stage_events:
                            self._record_event(event)
                        self._record_checkpoint_events()
                        if request.result is not None:
                            request.result["events"] = self._copy(stage_events)
                            with self._guard:
                                if self._last_recovery is not None:
                                    self._last_recovery["events"] = self._copy(
                                        stage_events
                                    )
                        elif request.error is not None:
                            with self._guard:
                                if self._last_recovery is not None:
                                    self._last_recovery["events"] = self._copy(
                                        stage_events
                                    )
                except Exception as error:
                    request.error = error
                    with self._guard:
                        self._last_recovery = {
                            "worker_id": request.worker_id,
                            "status": "failed",
                            "error": str(error),
                            "completed_at_utc": self._now(),
                        }
                        self._record_event(
                            {
                                "type": "recovery_event",
                                "event": "recovery_failed",
                                "worker_id": request.worker_id,
                                "occurred_at_utc": self._now(),
                                "stage": "checkpoint_or_request",
                                "reason": str(error),
                            }
                        )
                    self._record_checkpoint_events()
            request.completed.set()

    def inject_fault(self, worker_id: str, timeout: float = 30.0) -> dict[str, Any]:
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id must be a non-empty string")
        with self._guard:
            if self._status != "running":
                raise RuntimeUnavailableError(
                    self._error or "monitor runtime is unavailable"
                )
            active = {
                worker_id for worker_id in self._pool.active_worker_ids()
            }
            if worker_id not in active:
                raise UnknownWorkerError(f"worker is not currently active: {worker_id}")
            request = _FaultRequest(worker_id)
            self._fault_requests.put(request)
        if not request.completed.wait(timeout):
            raise TimeoutError("timed out waiting for the recovery operation")
        if request.error is not None:
            raise RecoveryRequestError(
                str(request.error), self._copy(request.events)
            ) from request.error
        if request.result is None:
            raise RuntimeUnavailableError("recovery completed without an outcome")
        return self._copy(request.result)

    def snapshot(self) -> dict[str, Any]:
        with self._guard:
            self._refresh_embedded_fault_request()
            twin_snapshot = self._twins.snapshot()
            worker_ids = sorted(
                set(self._latest_telemetry)
                | {item["worker_id"] for item in twin_snapshot["twins"]}
            )
            workers = []
            twin_by_id = {item["worker_id"]: item for item in twin_snapshot["twins"]}
            for worker_id in worker_ids:
                workers.append(
                    {
                        "worker_id": worker_id,
                        "telemetry": self._copy(
                            self._latest_telemetry.get(worker_id)
                        ),
                        "digital_twin": self._copy(twin_by_id.get(worker_id)),
                        "risk": self._copy(self._risks.get(worker_id)),
                    }
                )
            embedded_assessments = []
            for task_id in sorted(EMBEDDED_TASK_IDS):
                latest = self._embedded_latest.get(
                    ("freertos-qemu-rv32", task_id)
                )
                assessment = self._embedded_risk_policy.assess(task_id, latest)
                self._record_embedded_risk_transition(
                    task_id,
                    assessment,
                    latest,
                    occurred_at_utc=(
                        assessment["assessed_at_utc"]
                        if assessment["telemetry_freshness"] == "stale"
                        else None
                    ),
                )
                embedded_assessments.append(assessment)
            checkpoints = self._checkpoints.list_checkpoints()
            return {
                "status": self._status,
                "error": self._error,
                "started_at_utc": self._started_at_utc,
                "configured_worker_count": self.workers,
                "active_worker_count": (
                    len(self._pool.active_worker_ids())
                    if self._pool is not None
                    else 0
                ),
                "sample_interval_seconds": self.sample_interval,
                "workers": workers,
                "digital_twin": twin_snapshot,
                "risk_assessments": [
                    self._copy(self._risks[key])
                    for key in sorted(self._risks)
                ],
                "checkpoint_status": {
                    "storage_usage_bytes": self._checkpoints.storage_usage_bytes(),
                    "storage_budget_bytes": self.checkpoint_budget_bytes,
                    "checkpoint_count": len(checkpoints),
                },
                "checkpoints": checkpoints,
                "history": self._copy(list(self._history)[-120:]),
                "embedded_tasks": {
                    "latest": self._copy(list(self._embedded_latest.values())),
                    "history": self._copy(list(self._embedded_history)[-120:]),
                },
                "embedded_risk_events": self._copy(
                    list(self._embedded_risk_events)
                ),
                "embedded_ml": self._embedded_ml_shadow.snapshot(),
                "embedded_checkpoint_status": [
                    self._copy(self._embedded_checkpoint_status[task_id])
                    for task_id in sorted(self._embedded_checkpoint_status)
                ],
                "embedded_checkpoint_storage": self._copy(
                    self._embedded_checkpoint_storage
                ),
                "embedded_fault_injection": self._copy(
                    self._embedded_fault_injection
                ),
                "embedded_fault_runs": self._copy(
                    list(self._embedded_fault_runs)
                ),
                "embedded_risk_assessments": embedded_assessments,
                "recovery": self._copy(self._last_recovery),
                "events": self._copy(list(self._events)[-100:]),
            }

    def _refresh_embedded_fault_request(self) -> None:
        request = self._embedded_fault_injection
        if request is None or request["status"] in {
            "application_state_recovery_verified",
            "failed",
        }:
            return
        if request["status"] == "queued_for_uart":
            _, reason = self._embedded_fault_eligibility(
                request["bridge_session_id"]
            )
            if reason is not None:
                request["status"] = "failed"
                request["updated_at_utc"] = self._now()
                request["error"] = reason
                self._record_event(
                    {
                        "type": "embedded_fault_request",
                        "event": "fault_injection_rejected",
                        "task_id": request["task_id"],
                        "request_id": request["request_id"],
                        "bridge_session_id": request["bridge_session_id"],
                        "reason": reason,
                        "occurred_at_utc": request["updated_at_utc"],
                    }
                )
                return
        updated_at = datetime.fromisoformat(request["updated_at_utc"])
        if (
            datetime.now(timezone.utc) - updated_at.astimezone(timezone.utc)
        ).total_seconds() > 15.0:
            request["status"] = "failed"
            request["updated_at_utc"] = self._now()
            request["error"] = self._embedded_fault_timeout_reason(
                request["status"]
            )
            self._record_event(
                {
                    "type": "embedded_fault_request",
                    "event": "fault_injection_timed_out",
                    "task_id": request["task_id"],
                    "request_id": request["request_id"],
                    "bridge_session_id": request["bridge_session_id"],
                    "reason": request["error"],
                    "occurred_at_utc": request["updated_at_utc"],
                }
            )

    @staticmethod
    def _embedded_fault_timeout_reason(status: str) -> str:
        if status == "queued_for_uart":
            return "timed out waiting for the active bridge to dispatch the UART command"
        if status == "uart_command_dispatched":
            return "timed out waiting for firmware fault acknowledgement"
        if status in {"fault_command_received", "fault_observed"}:
            return "timed out waiting for fresh below-threshold telemetry"
        if status in {"elevated_risk_observed", "checkpoint_requested"}:
            return "timed out waiting for firmware checkpoint-save acknowledgement"
        if status == "checkpoint_saved":
            return "timed out waiting for application-state restore verification"
        if status in {
            "restore_started",
            "replacement_starting",
            "replacement_running",
            "restore_verified",
            "restored_progress_verified",
        }:
            return "timed out waiting for verified restored-state progress and completion"
        return "timed out waiting for embedded fault/recovery confirmation"

    def ingest_embedded_telemetry(
        self, bridge_session_id: str, telemetry: object
    ) -> dict[str, Any]:
        if not isinstance(bridge_session_id, str):
            raise EmbeddedTelemetryError("bridge_session_id must be a UUID string")
        try:
            if str(uuid.UUID(bridge_session_id)) != bridge_session_id:
                raise ValueError("UUID must use canonical form")
        except ValueError as error:
            raise EmbeddedTelemetryError(
                "bridge_session_id must be a canonical UUID string"
            ) from error
        sample = validate_task_telemetry(telemetry)
        with self._guard:
            if self._status != "running":
                raise RuntimeUnavailableError(
                    self._error or "monitor runtime is unavailable"
                )
            if sample["task_id"] in EMBEDDED_TASK_IDS:
                if (
                    self._embedded_bridge_session_id is not None
                    and self._embedded_bridge_session_id != bridge_session_id
                ):
                    self._embedded_latest.clear()
                    self._embedded_checkpoint_storage = None
                    self._embedded_checkpoint_status.clear()
                    self._embedded_risk_categories.clear()
                    self._embedded_ml_shadow.reset_session()
                self._embedded_bridge_session_id = bridge_session_id
            request = self._embedded_fault_injection
            if (
                sample["task_id"] == "sensor-sampler"
                and request is not None
                and request["bridge_session_id"] == bridge_session_id
                and request["status"]
                not in {"application_state_recovery_verified", "failed", "queued_for_uart"}
                and sample.get("run_id") != request["run_id"]
            ):
                return {
                    "success": True,
                    "accepted": False,
                    "stale": True,
                    "reason": "sensor telemetry does not belong to the active recovery run",
                }
            dedupe_key = (
                bridge_session_id,
                sample["task_id"],
                sample["execution_count"],
            )
            previous = self._embedded_seen.get(dedupe_key)
            if previous is not None:
                if previous != sample:
                    raise EmbeddedTelemetryConflictError(
                        "a different sample already used this task execution identity"
                    )
                return {
                    "success": True,
                    "duplicate": True,
                    "task": self._copy(
                        self._embedded_latest[(sample["target"], sample["task_id"])]
                    ),
                }
            order_key = (bridge_session_id, sample["task_id"])
            previous_execution = self._embedded_last_execution.get(order_key)
            if previous_execution is not None:
                execution_delta = (
                    sample["execution_count"] - previous_execution
                ) & 0xFFFFFFFF
                if not 0 < execution_delta < 0x80000000:
                    raise EmbeddedTelemetryOutOfOrderError(
                        "task execution_count did not advance within this bridge session"
                    )
            received = {
                **sample,
                "bridge_session_id": bridge_session_id,
                "received_at_utc": self._now(),
            }
            self._embedded_seen[dedupe_key] = sample
            self._embedded_last_execution[order_key] = sample["execution_count"]
            if len(self._embedded_seen) > 10000:
                self._embedded_seen.pop(next(iter(self._embedded_seen)))
            self._embedded_latest[(sample["target"], sample["task_id"])] = received
            self._embedded_history.append(received)
            embedded_assessment = self._embedded_risk_policy.assess(
                sample["task_id"], received
            )
            if sample["task_id"] in EMBEDDED_TASK_IDS:
                self._embedded_ml_shadow.ingest(
                    sample,
                    bridge_session_id=bridge_session_id,
                    observed_at_utc=received["received_at_utc"],
                    assessment=embedded_assessment,
                )
            self._record_embedded_risk_transition(
                sample["task_id"],
                embedded_assessment,
                received,
                occurred_at_utc=received["received_at_utc"],
            )
            accepted_event = {
                "type": "embedded_task_telemetry",
                **received,
                "embedded_risk_assessment": embedded_assessment,
            }
            self._update_embedded_fault_injection_from_telemetry(
                received, embedded_assessment
            )
            self._record_event(
                accepted_event
            )
            return {"success": True, "duplicate": False, "task": self._copy(received)}

    def ingest_embedded_task_event(
        self, bridge_session_id: str, event: object
    ) -> dict[str, Any]:
        if not isinstance(bridge_session_id, str):
            raise EmbeddedTelemetryError("bridge_session_id must be a UUID string")
        try:
            if str(uuid.UUID(bridge_session_id)) != bridge_session_id:
                raise ValueError("UUID must use canonical form")
        except ValueError as error:
            raise EmbeddedTelemetryError(
                "bridge_session_id must be a canonical UUID string"
            ) from error
        record = validate_task_event(event)
        with self._guard:
            if self._status != "running":
                raise RuntimeUnavailableError(
                    self._error or "monitor runtime is unavailable"
                )
            accepted = {
                "type": "embedded_task_event",
                **record,
                "bridge_session_id": bridge_session_id,
                "occurred_at_utc": self._now(),
            }
            request = self._embedded_fault_injection
            if (
                record["task_id"] == "sensor-sampler"
                and request is not None
                and request["bridge_session_id"] == bridge_session_id
                and request["status"]
                not in {"application_state_recovery_verified", "failed"}
                and record.get("run_id") != request["run_id"]
            ):
                return {
                    "success": True,
                    "accepted": False,
                    "stale": True,
                    "reason": "firmware acknowledgement does not belong to the active recovery run",
                }
            self._embedded_ml_shadow.observe_event(
                bridge_session_id, record
            )
            self._update_embedded_fault_injection(accepted)
            if record["event"] == "checkpoint_saved":
                state = {
                    field: record[field]
                    for field in ("counter", "accumulator", "integrity_hash")
                }
                self._embedded_checkpoint_storage = {
                    "task_id": record["task_id"],
                    "checkpoint_id": record.get("run_id"),
                    "run_id": record.get("run_id"),
                    "bridge_session_id": bridge_session_id,
                    "created_at_utc": accepted["occurred_at_utc"],
                    "size_bytes": record["checkpoint_size_bytes"],
                    "storage_used_bytes": record["checkpoint_size_bytes"],
                    "storage_budget_bytes": None,
                    "storage_kind": "volatile_firmware_ram",
                    "storage_note": (
                        "One firmware RAM checkpoint slot; no configurable "
                        "storage budget or persistent checkpoint files."
                    ),
                    "application_state": state,
                }
            if record["event"] in {
                "checkpoint_requested",
                "checkpoint_saved",
                "checkpoint_failed",
                "restore_started",
                "task_restart_started",
                "task_restarted",
                "restore_succeeded",
                "restore_failed",
                "task_progress",
                "recovery_completed",
                "recovery_failed",
                "fault_injection_requested",
                "fault_injected",
            }:
                self._embedded_checkpoint_status[record["task_id"]] = accepted
            self._record_event(accepted)
            return {"success": True, "event": self._copy(accepted)}

    def _embedded_fault_eligibility(
        self, bridge_session_id: str | None = None
    ) -> tuple[str | None, str | None]:
        previous_request = self._embedded_fault_injection
        configured_threshold = (
            self._embedded_risk_policy.minimum_stack_high_water_words
        )
        if configured_threshold < EMBEDDED_RECOVERY_DEMO_MIN_THRESHOLD_WORDS:
            return None, (
                "the bounded QEMU recovery demonstration requires "
                "--embedded-stack-reserve-threshold-words "
                f"{EMBEDDED_RECOVERY_DEMO_MIN_THRESHOLD_WORDS}; the measured "
                "fault frame leaves 95 stack words, but the configured threshold "
                f"is {configured_threshold}"
            )
        if previous_request is not None:
            if previous_request["status"] == "failed":
                active_session = (
                    bridge_session_id or self._embedded_bridge_session_id
                )
                if active_session == previous_request["bridge_session_id"]:
                    return None, (
                        "the previous embedded recovery failed in this QEMU "
                        "bridge session; restart the bridge and wait for fresh "
                        "telemetry before retrying"
                    )
            if previous_request["status"] == "application_state_recovery_verified":
                progress = next(
                    (
                        item
                        for item in reversed(previous_request["events"])
                        if item["event"] == "task_progress"
                    ),
                    None,
                )
                completed_at = previous_request.get("recovery_completed_at_utc")
                sensor_sample = self._embedded_latest.get(
                    ("freertos-qemu-rv32", "sensor-sampler")
                )
                if (
                    progress is None
                    or completed_at is None
                    or sensor_sample is None
                    or sensor_sample.get("run_id") != previous_request["run_id"]
                    or sensor_sample.get("execution_count", 0)
                    <= progress.get("counter", 0)
                    or sensor_sample.get("received_at_utc", "") <= completed_at
                ):
                    return None, (
                        "waiting for fresh sensor-sampler telemetry showing progress "
                        "after the previous verified recovery"
                    )
        samples = {
            task_id: self._embedded_latest.get(
                ("freertos-qemu-rv32", task_id)
            )
            for task_id in EMBEDDED_TASK_IDS
        }
        if any(sample is None for sample in samples.values()):
            return None, "fresh telemetry from both embedded tasks is required"
        sessions = {
            sample["bridge_session_id"]
            for sample in samples.values()
            if sample is not None
        }
        if len(sessions) != 1:
            return None, "both embedded tasks must report from the same active QEMU bridge"
        session = next(iter(sessions))
        if bridge_session_id is not None and session != bridge_session_id:
            return None, "request belongs to a different QEMU bridge session"
        for task_id, sample in samples.items():
            assessment = self._embedded_risk_policy.assess(task_id, sample)
            if (
                assessment["risk_category"] != "LOW"
                or assessment["telemetry_freshness"] != "fresh"
                or assessment["observed_inputs"].get("task_state") != "running"
            ):
                return None, (
                    f"{task_id} must have fresh running LOW-risk telemetry "
                    "before stack pressure can be injected"
                )
        return session, None

    def request_embedded_fault(self, task_id: str) -> dict[str, Any]:
        if task_id != "sensor-sampler":
            raise EmbeddedFaultRequestError(
                "bounded stack-pressure injection is supported only for sensor-sampler"
            )
        with self._guard:
            if self._status != "running":
                raise RuntimeUnavailableError(
                    self._error or "monitor runtime is unavailable"
                )
            bridge_session_id, reason = self._embedded_fault_eligibility()
            if reason is not None or bridge_session_id is None:
                raise EmbeddedFaultRequestError(reason or "embedded telemetry is unavailable")
            current = self._embedded_fault_injection
            if current is not None and current["status"] not in {
                "application_state_recovery_verified",
                "failed",
            }:
                raise EmbeddedFaultRequestError(
                    "an embedded fault/recovery request is already in progress"
                )
            run_id = str(uuid.uuid4())
            requested = {
                "request_id": run_id,
                "run_id": run_id,
                "checkpoint_id": run_id,
                "task_id": task_id,
                "bridge_session_id": bridge_session_id,
                "status": "queued_for_uart",
                "requested_at_utc": self._now(),
                "updated_at_utc": self._now(),
                "events": [],
                "explanation": (
                    "A bounded UART stack-pressure fault will be sent to the "
                    "sensor-sampler task; only its application state is eligible "
                    "for checkpoint and restore."
                ),
            }
            self._embedded_fault_injection = requested
            self._embedded_fault_runs.append(requested)
            self._record_event(
                {
                    "type": "embedded_fault_request",
                    "event": "fault_injection_queued",
                    "task_id": task_id,
                    "request_id": requested["request_id"],
                    "run_id": requested["run_id"],
                    "bridge_session_id": bridge_session_id,
                    "occurred_at_utc": requested["requested_at_utc"],
                }
            )
            return self._copy(requested)

    def take_embedded_uart_command(
        self, bridge_session_id: str
    ) -> dict[str, Any] | None:
        with self._guard:
            request = self._embedded_fault_injection
            if (
                request is None
                or request["status"] != "queued_for_uart"
                or request["bridge_session_id"] != bridge_session_id
            ):
                return None
            session, reason = self._embedded_fault_eligibility(bridge_session_id)
            if reason is not None or session is None:
                request["status"] = "failed"
                request["updated_at_utc"] = self._now()
                request["error"] = reason or "embedded telemetry is no longer eligible"
                self._record_event(
                    {
                        "type": "embedded_fault_request",
                        "event": "fault_injection_rejected",
                        "task_id": request["task_id"],
                        "request_id": request["request_id"],
                        "bridge_session_id": bridge_session_id,
                        "reason": request["error"],
                        "occurred_at_utc": request["updated_at_utc"],
                    }
                )
                return None
            request["status"] = "uart_command_dispatched"
            request["updated_at_utc"] = self._now()
            request["events"].append(
                {
                    "event": "uart_command_dispatched",
                    "occurred_at_utc": request["updated_at_utc"],
                    "run_id": request["run_id"],
                    "checkpoint_id": request["run_id"],
                }
            )
            return {
                "request_id": request["request_id"],
                "run_id": request["run_id"],
                "command": (
                    f"INJECT_STACK_PRESSURE {request['task_id']} "
                    f"{request['run_id']}"
                ),
            }

    def _update_embedded_fault_injection(
        self, event: dict[str, Any]
    ) -> None:
        request = self._embedded_fault_injection
        if (
            request is None
            or request["task_id"] != event["task_id"]
            or request["bridge_session_id"] != event["bridge_session_id"]
            or request["status"] in {"application_state_recovery_verified", "failed"}
            or event.get("run_id") != request["run_id"]
        ):
            return
        transitions = {
            "fault_injection_requested": "fault_command_received",
            "fault_injected": "fault_observed",
            "checkpoint_requested": "checkpoint_requested",
            "checkpoint_saved": "checkpoint_saved",
            "checkpoint_failed": "failed",
            "restore_started": "restore_started",
            "task_restart_started": "replacement_starting",
            "task_restarted": "replacement_running",
            "restore_succeeded": "restore_verified",
            "restore_failed": "failed",
            "task_progress": "restored_progress_verified",
            "recovery_completed": "application_state_recovery_verified",
            "recovery_failed": "failed",
        }
        status = transitions.get(event["event"])
        if status is None:
            return
        request["status"] = status
        request["updated_at_utc"] = event["occurred_at_utc"]
        request["events"].append(
            {
                "event": event["event"],
                "occurred_at_utc": event["occurred_at_utc"],
                "run_id": event["run_id"],
                "checkpoint_id": request["run_id"],
                **(
                    {"counter": event["counter"]}
                    if "counter" in event
                    else {}
                ),
                **(
                    {"accumulator": event["accumulator"]}
                    if "accumulator" in event
                    else {}
                ),
                **(
                    {"integrity_hash": event["integrity_hash"]}
                    if "integrity_hash" in event
                    else {}
                ),
                **(
                    {
                        "checkpoint_size_bytes": event[
                            "checkpoint_size_bytes"
                        ]
                    }
                    if "checkpoint_size_bytes" in event
                    else {}
                ),
            }
        )
        if event["event"] == "checkpoint_saved":
            request["checkpoint_saved_state"] = {
                field: event[field]
                for field in (
                    "counter",
                    "accumulator",
                    "integrity_hash",
                    "checkpoint_size_bytes",
                )
            }
        elif event["event"] == "restore_succeeded":
            request["restored_state"] = {
                field: event[field]
                for field in ("counter", "accumulator", "integrity_hash")
            }
        elif event["event"] == "task_progress":
            request["restored_progress"] = {
                field: event[field]
                for field in ("counter", "accumulator", "integrity_hash")
            }
        elif event["event"] == "recovery_completed":
            request["recovery_completed_at_utc"] = event["occurred_at_utc"]

    def _record_embedded_risk_transition(
        self,
        task_id: str,
        assessment: dict[str, Any],
        sample: dict[str, Any] | None,
        *,
        occurred_at_utc: str | None = None,
    ) -> None:
        category = assessment["risk_category"]
        previous = self._embedded_risk_categories.get(task_id, "UNKNOWN")
        if category == previous:
            return
        observed = assessment.get("observed_inputs", {})
        configuration = assessment.get("policy_configuration", {})
        self._embedded_risk_events.append(
            {
                "task_id": task_id,
                "previous_category": previous,
                "risk_category": category,
                "occurred_at_utc": occurred_at_utc
                or assessment["assessed_at_utc"],
                "bridge_session_id": (
                    sample.get("bridge_session_id")
                    if sample is not None
                    else self._embedded_bridge_session_id
                ),
                "execution_count": observed.get("execution_count"),
                "run_id": observed.get("run_id"),
                "stack_reserve_words": observed.get(
                    "stack_high_water_mark_words"
                ),
                "threshold_words": configuration.get(
                    "minimum_stack_high_water_words"
                ),
                "risk_index": assessment["risk_index"],
                "reason": assessment["reason"],
            }
        )
        self._embedded_risk_categories[task_id] = category

    def _update_embedded_fault_injection_from_telemetry(
        self,
        sample: dict[str, Any],
        assessment: dict[str, Any],
    ) -> None:
        request = self._embedded_fault_injection
        if (
            request is None
            or request["task_id"] != sample["task_id"]
            or request["bridge_session_id"] != sample["bridge_session_id"]
            or sample.get("run_id") != request["run_id"]
            or request["status"] in {"application_state_recovery_verified", "failed"}
            or assessment["risk_category"] != "ELEVATED"
            or assessment["telemetry_freshness"] != "fresh"
            or request.get("elevated_assessment") is not None
        ):
            return
        request["elevated_assessment"] = self._copy(assessment)
        request["updated_at_utc"] = self._now()
        if request["status"] in {
            "uart_command_dispatched",
            "fault_command_received",
            "fault_observed",
        }:
            request["status"] = "elevated_risk_observed"
        request["events"].append(
            {
                "event": "embedded_risk_updated",
                "occurred_at_utc": request["updated_at_utc"],
                "run_id": request["run_id"],
                "checkpoint_id": request["run_id"],
                "execution_count": sample["execution_count"],
                "risk_category": assessment["risk_category"],
                "risk_index": assessment["risk_index"],
                "stack_reserve_words": assessment["observed_inputs"][
                    "stack_high_water_mark_words"
                ],
                "threshold_words": assessment["policy_configuration"][
                    "minimum_stack_high_water_words"
                ],
                "reason": assessment["reason"],
            }
        )

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self._guard:
            return self._copy(list(self._events)[-limit:])
