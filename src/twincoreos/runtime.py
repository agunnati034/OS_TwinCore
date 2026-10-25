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
from twincoreos.monitor import WorkerPool
from twincoreos.recovery import RecoveryCoordinator, RecoveryError
from twincoreos.risk_predictor import BaselineRiskPredictor


class RuntimeUnavailableError(RuntimeError):
    """The monitor runtime is not ready to accept an operation."""


class UnknownWorkerError(ValueError):
    """A requested worker ID is not currently active."""


class EmbeddedTelemetryConflictError(ValueError):
    """A repeated telemetry identity contains different measurements."""


class EmbeddedTelemetryOutOfOrderError(ValueError):
    """An embedded task sample regressed within one bridge session."""


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
        self._predictor = BaselineRiskPredictor(
            assessment_validity_seconds=stale_after
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
        self._embedded_history: deque[dict[str, Any]] = deque(maxlen=500)
        self._embedded_seen: dict[tuple[str, str, int], dict[str, Any]] = {}
        self._embedded_last_execution: dict[tuple[str, str], int] = {}
        self._embedded_checkpoint_status: dict[str, dict[str, Any]] = {}
        self._embedded_risk_policy = EmbeddedRiskPolicy(
            minimum_stack_high_water_words=embedded_stack_reserve_threshold_words,
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
                "embedded_checkpoint_status": [
                    self._copy(self._embedded_checkpoint_status[task_id])
                    for task_id in sorted(self._embedded_checkpoint_status)
                ],
                "embedded_risk_assessments": [
                    self._embedded_risk_policy.assess(
                        task_id,
                        self._embedded_latest.get(("freertos-qemu-rv32", task_id)),
                    )
                    for task_id in sorted(EMBEDDED_TASK_IDS)
                ],
                "recovery": self._copy(self._last_recovery),
                "events": self._copy(list(self._events)[-100:]),
            }

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
            self._record_event(
                {
                    "type": "embedded_task_telemetry",
                    **received,
                }
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
            if record["event"] in {
                "checkpoint_requested",
                "checkpoint_saved",
                "checkpoint_failed",
            }:
                self._embedded_checkpoint_status[record["task_id"]] = accepted
            self._record_event(accepted)
            return {"success": True, "event": self._copy(accepted)}

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self._guard:
            return self._copy(list(self._events)[-limit:])
