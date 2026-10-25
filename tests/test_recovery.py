from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import psutil

from twincoreos.checkpoint_manager import CheckpointManager
from twincoreos.digital_twin import DigitalTwinEngine, HealthState
from twincoreos.monitor import WorkerPool
from twincoreos.recovery import RecoveryCoordinator, RecoveryError


def _observation(worker_id: str, pid: int) -> dict[str, object]:
    return {
        "task_id": worker_id,
        "pid": pid,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "monitor_elapsed_seconds": 0.1,
        "cpu_utilization_percent_one_core": 10.0,
        "memory_rss_bytes": 1024,
        "process_status": psutil.STATUS_RUNNING,
        "measurement_errors": {},
    }


class RecoveryIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.manager = CheckpointManager(Path(self.temporary.name) / "store", 100_000)
        self.pool = WorkerPool(workers=2, memory_mib=1)
        self.pool.__enter__()
        self.twins = DigitalTwinEngine()
        for worker in self.pool._processes:
            self.twins.update(_observation(worker.task_id, worker.child.pid))
        self.target = self.pool._processes[0]
        self.other = self.pool._processes[1]

    def tearDown(self) -> None:
        self.pool.close()
        self.temporary.cleanup()

    def _checkpoint_target(self) -> dict[str, object]:
        pid, state = self.pool.advance_application_state(self.target.task_id, 3)
        return self.manager.create_checkpoint(self.target.task_id, pid, state)

    def test_recovery_terminates_only_target_and_restores_then_advances(self) -> None:
        checkpoint = self._checkpoint_target()
        target_pid = self.target.child.pid
        other_pid = self.other.child.pid
        result = RecoveryCoordinator(
            self.pool, self.twins, self.manager
        ).recover(self.target.task_id)

        self.assertTrue(result["success"])
        self.assertEqual(result["failed_pid"], target_pid)
        self.assertFalse(psutil.pid_exists(target_pid))
        self.assertNotEqual(result["replacement_pid"], target_pid)
        self.assertEqual(result["replacement_pid"], self.pool.application_state(
            result["replacement_worker_id"]
        )[0])
        self.assertEqual(result["restored_state"], checkpoint["application_state"])
        self.assertEqual(result["continued_state"]["counter"], 4)
        self.assertEqual(result["continued_state"]["accumulator"], 10)
        self.assertTrue(psutil.pid_exists(other_pid))
        self.assertEqual(
            self.twins.get(self.target.task_id).health_state, HealthState.FAILED
        )

    def test_recovery_events_are_ordered_and_timestamped(self) -> None:
        self._checkpoint_target()
        coordinator = RecoveryCoordinator(self.pool, self.twins, self.manager)
        coordinator.recover(self.target.task_id)
        events = coordinator.drain_events()
        names = [event["event"] for event in events]
        self.assertEqual(
            names,
            [
                "fault_requested",
                "worker_terminated",
                "recovery_started",
                "checkpoint_selected",
                "restore_succeeded",
                "recovery_completed",
            ],
        )
        self.assertTrue(
            all(
                datetime.fromisoformat(event["occurred_at_utc"]).tzinfo is not None
                for event in events
            )
        )

    def test_missing_checkpoint_fails_without_terminating_worker(self) -> None:
        coordinator = RecoveryCoordinator(self.pool, self.twins, self.manager)
        with self.assertRaisesRegex(RecoveryError, "no valid checkpoint"):
            coordinator.recover(self.target.task_id)
        self.assertTrue(psutil.pid_exists(self.target.child.pid))
        self.assertEqual(
            self.twins.get(self.target.task_id).health_state, HealthState.HEALTHY
        )
        self.assertEqual(coordinator.drain_events()[-1]["event"], "recovery_failed")

    def test_corrupt_checkpoint_is_rejected_without_replacement(self) -> None:
        checkpoint = self._checkpoint_target()
        path = self.manager.storage_dir / f"{checkpoint['checkpoint_id']}.json"
        document = json.loads(path.read_text())
        document["application_state"]["counter"] += 10
        path.write_text(json.dumps(document))

        coordinator = RecoveryCoordinator(self.pool, self.twins, self.manager)
        with self.assertRaisesRegex(RecoveryError, "no valid checkpoint"):
            coordinator.recover(self.target.task_id)
        self.assertTrue(psutil.pid_exists(self.target.child.pid))
        self.assertFalse(
            any(worker.task_id.startswith(f"{self.target.task_id}-replacement-")
                for worker in self.pool._processes)
        )

    def test_replacement_start_failure_is_reported(self) -> None:
        self._checkpoint_target()
        coordinator = RecoveryCoordinator(self.pool, self.twins, self.manager)
        with patch.object(self.pool, "start_replacement", side_effect=RuntimeError("startup")):
            with self.assertRaisesRegex(RecoveryError, "startup"):
                coordinator.recover(self.target.task_id)
        self.assertEqual(coordinator.drain_events()[-1]["event"], "recovery_failed")

    def test_restore_failure_cleans_replacement(self) -> None:
        self._checkpoint_target()
        coordinator = RecoveryCoordinator(self.pool, self.twins, self.manager)
        with patch.object(
            self.pool, "restore_application_state", side_effect=RuntimeError("restore")
        ):
            with self.assertRaisesRegex(RecoveryError, "restore"):
                coordinator.recover(self.target.task_id)
        self.assertFalse(
            any(worker.task_id.startswith(f"{self.target.task_id}-replacement-")
                for worker in self.pool._processes)
        )

    def test_restore_timeout_cleans_replacement(self) -> None:
        self._checkpoint_target()
        coordinator = RecoveryCoordinator(self.pool, self.twins, self.manager)
        with patch.object(
            self.manager, "restore_checkpoint", side_effect=TimeoutError("timeout")
        ):
            with self.assertRaisesRegex(RecoveryError, "timeout"):
                coordinator.recover(self.target.task_id)
        self.assertFalse(
            any(worker.task_id.startswith(f"{self.target.task_id}-replacement-")
                for worker in self.pool._processes)
        )


if __name__ == "__main__":
    unittest.main()
