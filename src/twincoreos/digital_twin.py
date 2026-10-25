"""Per-worker host-process digital twins derived from observed telemetry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import math
import time
from typing import Any, Mapping

import psutil


class HealthState(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


class InvalidObservationError(ValueError):
    """A telemetry observation is invalid or out of order."""


@dataclass
class TwinState:
    worker_id: str
    pid: int
    latest_observation: dict[str, Any] | None
    previous_observation: dict[str, Any] | None
    observed_at_monotonic: float | None
    health_state: HealthState
    previous_health_state: HealthState | None
    stale: bool
    failure_reason: str | None = None


class DigitalTwinEngine:
    """Maintain isolated state for workers observed by the host monitor.

    HEALTHY requires a running process and available CPU utilization and RSS.
    DEGRADED means telemetry is stale/unavailable or the process is not running.
    FAILED is reserved for an observed terminal process status or an explicit
    worker-exit report from the supervisor. Missing metrics alone never fail a
    worker. Once FAILED, a twin remains FAILED for that worker identity.
    """

    def __init__(self, stale_after_seconds: float = 3.0) -> None:
        if not math.isfinite(stale_after_seconds) or stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be finite and positive")
        self.stale_after_seconds = stale_after_seconds
        self._twins: dict[str, TwinState] = {}

    @staticmethod
    def _parse_timestamp(value: object) -> datetime:
        if not isinstance(value, str):
            raise InvalidObservationError("captured_at_utc must be an ISO timestamp")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise InvalidObservationError("captured_at_utc is invalid") from error
        if parsed.tzinfo is None:
            raise InvalidObservationError("captured_at_utc must include a timezone")
        return parsed

    @staticmethod
    def _is_number(value: object) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    def _health_for(self, observation: Mapping[str, Any]) -> HealthState:
        status = observation.get("process_status")
        if status in (psutil.STATUS_DEAD, psutil.STATUS_ZOMBIE):
            return HealthState.FAILED
        if status != psutil.STATUS_RUNNING:
            return HealthState.DEGRADED
        if observation.get("measurement_errors"):
            return HealthState.DEGRADED
        if not self._is_number(observation.get("cpu_utilization_percent_one_core")):
            return HealthState.DEGRADED
        if not self._is_number(observation.get("memory_rss_bytes")):
            return HealthState.DEGRADED
        return HealthState.HEALTHY

    def update(
        self,
        observation: Mapping[str, Any],
        observed_at_monotonic: float | None = None,
    ) -> TwinState:
        worker_id = observation.get("task_id")
        pid = observation.get("pid")
        if not isinstance(worker_id, str) or not worker_id:
            raise InvalidObservationError("task_id must be a non-empty string")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise InvalidObservationError("pid must be a positive integer")
        captured_at = self._parse_timestamp(observation.get("captured_at_utc"))
        monitor_elapsed = observation.get("monitor_elapsed_seconds")
        if (
            not self._is_number(monitor_elapsed)
            or not math.isfinite(monitor_elapsed)
            or monitor_elapsed < 0
        ):
            raise InvalidObservationError(
                "monitor_elapsed_seconds must be a non-negative number"
            )
        cpu_percent = observation.get("cpu_utilization_percent_one_core")
        if cpu_percent is not None and (
            not self._is_number(cpu_percent)
            or not math.isfinite(cpu_percent)
            or cpu_percent < 0
        ):
            raise InvalidObservationError(
                "cpu utilization must be a finite non-negative number or null"
            )
        rss_bytes = observation.get("memory_rss_bytes")
        if rss_bytes is not None and (
            not isinstance(rss_bytes, int)
            or isinstance(rss_bytes, bool)
            or rss_bytes < 0
        ):
            raise InvalidObservationError(
                "memory_rss_bytes must be a non-negative integer or null"
            )
        process_status = observation.get("process_status")
        if process_status is not None and not isinstance(process_status, str):
            raise InvalidObservationError("process_status must be a string or null")
        errors = observation.get("measurement_errors", {})
        if not isinstance(errors, Mapping):
            raise InvalidObservationError("measurement_errors must be an object")
        received_at = (
            time.monotonic()
            if observed_at_monotonic is None
            else observed_at_monotonic
        )
        if not math.isfinite(received_at):
            raise InvalidObservationError("observed_at_monotonic must be finite")

        twin = self._twins.get(worker_id)
        if twin is not None:
            if twin.pid != pid:
                raise InvalidObservationError(
                    f"worker {worker_id} PID changed from {twin.pid} to {pid}"
                )
            previous_timestamp = self._parse_timestamp(
                twin.latest_observation["captured_at_utc"]
            ) if twin.latest_observation is not None else None
            if previous_timestamp is not None and captured_at <= previous_timestamp:
                raise InvalidObservationError(
                    f"out-of-order observation for worker {worker_id}"
                )
            if (
                twin.observed_at_monotonic is not None
                and received_at <= twin.observed_at_monotonic
            ):
                raise InvalidObservationError(
                    f"monotonic observation time did not advance for worker {worker_id}"
                )
            if twin.health_state == HealthState.FAILED:
                raise InvalidObservationError(
                    f"worker {worker_id} is already marked FAILED"
                )
            twin.previous_observation = twin.latest_observation
            twin.previous_health_state = twin.health_state
            twin.latest_observation = dict(observation)
            twin.observed_at_monotonic = received_at
            twin.stale = False
            twin.health_state = self._health_for(observation)
        else:
            twin = TwinState(
                worker_id=worker_id,
                pid=pid,
                latest_observation=dict(observation),
                previous_observation=None,
                observed_at_monotonic=received_at,
                health_state=self._health_for(observation),
                previous_health_state=None,
                stale=False,
            )
            self._twins[worker_id] = twin
        return twin

    def mark_failed(
        self,
        worker_id: str,
        pid: int,
        reason: str,
        observed_at_monotonic: float | None = None,
    ) -> TwinState:
        if not worker_id or pid <= 0:
            raise ValueError("worker_id and a positive PID are required")
        twin = self._twins.get(worker_id)
        if twin is None:
            twin = TwinState(
                worker_id=worker_id,
                pid=pid,
                latest_observation=None,
                previous_observation=None,
                observed_at_monotonic=None,
                health_state=HealthState.DEGRADED,
                previous_health_state=None,
                stale=False,
            )
            self._twins[worker_id] = twin
        elif twin.pid != pid:
            raise InvalidObservationError(
                f"worker {worker_id} PID changed from {twin.pid} to {pid}"
            )

        if twin.health_state != HealthState.FAILED:
            twin.previous_health_state = twin.health_state
        twin.health_state = HealthState.FAILED
        twin.stale = False
        twin.failure_reason = reason
        twin.observed_at_monotonic = (
            time.monotonic()
            if observed_at_monotonic is None
            else observed_at_monotonic
        )
        return twin

    def refresh(self, now_monotonic: float | None = None) -> None:
        now = time.monotonic() if now_monotonic is None else now_monotonic
        if not math.isfinite(now):
            raise ValueError("now_monotonic must be finite")
        for twin in self._twins.values():
            if twin.health_state == HealthState.FAILED:
                continue
            stale = (
                twin.observed_at_monotonic is None
                or now - twin.observed_at_monotonic > self.stale_after_seconds
            )
            if stale:
                if twin.health_state != HealthState.DEGRADED:
                    twin.previous_health_state = twin.health_state
                twin.health_state = HealthState.DEGRADED
            twin.stale = stale

    @staticmethod
    def _snapshot_twin(twin: TwinState) -> dict[str, Any]:
        observation = twin.latest_observation or {}
        return {
            "worker_id": twin.worker_id,
            "pid": twin.pid,
            "health_state": twin.health_state.value,
            "previous_health_state": (
                twin.previous_health_state.value
                if twin.previous_health_state is not None
                else None
            ),
            "stale": twin.stale,
            "latest_observation": (
                dict(twin.latest_observation)
                if twin.latest_observation is not None
                else None
            ),
            "previous_observation": (
                dict(twin.previous_observation)
                if twin.previous_observation is not None
                else None
            ),
            "cpu_utilization_percent_one_core": observation.get(
                "cpu_utilization_percent_one_core"
            ),
            "cpu_time_total_seconds": observation.get("cpu_time_total_seconds"),
            "memory_rss_bytes": observation.get("memory_rss_bytes"),
            "process_status": observation.get("process_status"),
            "observed_at_utc": observation.get("captured_at_utc"),
            "failure_reason": twin.failure_reason,
        }

    def snapshot(self, now_monotonic: float | None = None) -> dict[str, Any]:
        self.refresh(now_monotonic)
        return {
            "type": "digital_twin_snapshot",
            "stale_after_seconds": self.stale_after_seconds,
            "twins": [
                self._snapshot_twin(self._twins[worker_id])
                for worker_id in sorted(self._twins)
            ],
        }

    def get(self, worker_id: str) -> TwinState | None:
        return self._twins.get(worker_id)

    def states(self) -> tuple[TwinState, ...]:
        return tuple(self._twins[worker_id] for worker_id in sorted(self._twins))
