"""Controlled worker termination and verified application-state recovery."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid
from typing import Any

from twincoreos.checkpoint_manager import CheckpointError, CheckpointManager
from twincoreos.digital_twin import DigitalTwinEngine
from twincoreos.monitor import WorkerPool


class RecoveryError(RuntimeError):
    """A requested worker recovery could not be verified."""


class RecoveryCoordinator:
    def __init__(
        self,
        pool: WorkerPool,
        twins: DigitalTwinEngine,
        checkpoints: CheckpointManager,
    ) -> None:
        self.pool = pool
        self.twins = twins
        self.checkpoints = checkpoints
        self.events: list[dict[str, Any]] = []

    def _event(self, event: str, worker_id: str, **fields: Any) -> None:
        self.events.append(
            {
                "type": "recovery_event",
                "event": event,
                "worker_id": worker_id,
                "occurred_at_utc": datetime.now(timezone.utc).isoformat(),
                **fields,
            }
        )

    def drain_events(self) -> list[dict[str, Any]]:
        events, self.events = self.events, []
        return events

    def recover(self, worker_id: str, advance_steps: int = 1) -> dict[str, Any]:
        if (
            not isinstance(advance_steps, int)
            or isinstance(advance_steps, bool)
            or not 1 <= advance_steps <= 100_000
        ):
            raise ValueError("advance_steps must be an integer from 1 to 100000")
        self._event("fault_requested", worker_id, mechanism="terminate_worker")
        available = [
            item
            for item in self.checkpoints.list_checkpoints()
            if item["worker_id"] == worker_id
        ]
        if not available:
            self._event(
                "recovery_failed",
                worker_id,
                stage="checkpoint_selection",
                reason="no valid checkpoint exists for target worker",
            )
            raise RecoveryError("no valid checkpoint exists for target worker")
        selected = max(
            available,
            key=lambda item: (item["created_at_utc"], item["checkpoint_id"]),
        )

        try:
            failed_worker, failed_pid = self.pool.inject_termination(worker_id)
            self._event(
                "worker_terminated",
                worker_id,
                pid=failed_pid,
                exit_status=failed_worker.child.returncode,
            )
            self.twins.mark_failed(
                worker_id,
                failed_pid,
                "controlled fault injection: worker process terminated",
            )
        except Exception as error:
            self._event("recovery_failed", worker_id, stage="termination", reason=str(error))
            raise RecoveryError(f"could not terminate target worker: {error}") from error

        self._event("recovery_started", worker_id, failed_pid=failed_pid)
        replacement = None
        try:
            checkpoint_id = selected["checkpoint_id"]
            self._event(
                "checkpoint_selected",
                worker_id,
                checkpoint_id=checkpoint_id,
                created_at_utc=selected["created_at_utc"],
                size_bytes=selected["size_bytes"],
            )

            replacement_id = f"{worker_id}-replacement-{uuid.uuid4().hex[:12]}"
            replacement = self.pool.start_replacement(replacement_id)
            if replacement.child.pid == failed_pid:
                raise RecoveryError("replacement process reused failed PID")

            restored = self.checkpoints.restore_checkpoint(
                checkpoint_id,
                worker_id,
                lambda state: self.pool.restore_application_state(
                    replacement_id, state
                ),
            )
            restored_pid, restored_state = self.pool.application_state(replacement_id)
            if restored_pid != replacement.child.pid:
                raise RecoveryError("replacement PID changed during restore")
            if restored_state != restored["application_state"]:
                raise RecoveryError("restored state does not match checkpoint")
            self._event(
                "restore_succeeded",
                worker_id,
                checkpoint_id=checkpoint_id,
                replacement_worker_id=replacement_id,
                failed_pid=failed_pid,
                replacement_pid=restored_pid,
                restored_state=restored_state,
            )

            expected = dict(restored_state)
            expected["accumulator"] += (
                advance_steps * expected["counter"]
                + advance_steps * (advance_steps + 1) // 2
            )
            expected["counter"] += advance_steps
            advanced_pid, advanced_state = self.pool.advance_application_state(
                replacement_id, advance_steps
            )
            if advanced_pid != restored_pid or advanced_state != expected:
                raise RecoveryError(
                    "replacement did not continue from the restored application state"
                )
            self._event(
                "recovery_completed",
                worker_id,
                checkpoint_id=checkpoint_id,
                replacement_worker_id=replacement_id,
                failed_pid=failed_pid,
                replacement_pid=restored_pid,
                restored_state=restored_state,
                continued_state=advanced_state,
            )
            return {
                "worker_id": worker_id,
                "replacement_worker_id": replacement_id,
                "failed_pid": failed_pid,
                "replacement_pid": restored_pid,
                "checkpoint_id": checkpoint_id,
                "restored_state": restored_state,
                "continued_state": advanced_state,
                "success": True,
            }
        except Exception as error:
            if replacement is not None:
                try:
                    self.pool._stop_worker(replacement)
                finally:
                    if replacement in self.pool._processes:
                        self.pool._processes.remove(replacement)
            self._event(
                "recovery_failed",
                worker_id,
                stage="restore_or_verification",
                reason=str(error),
            )
            if isinstance(error, RecoveryError):
                raise
            if isinstance(error, CheckpointError):
                raise RecoveryError(f"checkpoint restore failed: {error}") from error
            raise RecoveryError(f"recovery failed: {error}") from error
