import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from twincoreos.checkpoint_manager import (
    CheckpointBudgetError,
    CheckpointError,
    CheckpointManager,
    CheckpointRejectedError,
)
from twincoreos.monitor import WorkerPool


class CheckpointManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.manager = CheckpointManager(self.directory, max_storage_bytes=100_000)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_create_and_read_checkpoint(self) -> None:
        created = self.manager.create_checkpoint(
            "worker-1",
            1234,
            {"counter": 12, "accumulator": 78},
        )

        restored = self.manager.read_checkpoint(created["checkpoint_id"])

        self.assertEqual(restored["schema_version"], 1)
        self.assertEqual(restored["worker_id"], "worker-1")
        self.assertEqual(restored["worker_pid"], 1234)
        self.assertEqual(restored["application_state"], {"counter": 12, "accumulator": 78})
        self.assertEqual(created["size_bytes"], self.manager.storage_usage_bytes())
        self.assertEqual([event["event"] for event in self.manager.drain_events()], ["created"])

    def test_listing_checkpoints_calculates_total_usage_once(self) -> None:
        for counter in range(1, 4):
            self.manager.create_checkpoint(
                "worker-1",
                1234,
                {
                    "counter": counter,
                    "accumulator": counter * (counter + 1) // 2,
                },
            )
        with mock.patch.object(
            self.manager,
            "storage_usage_bytes",
            wraps=self.manager.storage_usage_bytes,
        ) as usage:
            checkpoints = self.manager.list_checkpoints()
        self.assertEqual(len(checkpoints), 3)
        usage.assert_called_once()

    def test_listing_rejects_corrupted_checkpoints_and_records_event(self) -> None:
        checkpoint = self.manager.create_checkpoint(
            "worker-1", 1234, {"counter": 1, "accumulator": 1}
        )
        path = self.directory / f"{checkpoint['checkpoint_id']}.json"
        document = json.loads(path.read_text())
        document["application_state"]["counter"] = 99
        path.write_text(json.dumps(document))
        self.manager.drain_events()

        self.assertEqual(self.manager.list_checkpoints(), [])
        events = self.manager.drain_events()
        self.assertEqual([event["event"] for event in events], ["rejected"])

    def test_corrupt_checkpoint_is_rejected(self) -> None:
        checkpoint = self.manager.create_checkpoint(
            "worker-1", 1234, {"counter": 1, "accumulator": 1}
        )
        path = self.directory / f"{checkpoint['checkpoint_id']}.json"
        document = json.loads(path.read_text())
        document["application_state"]["counter"] = 99
        path.write_text(json.dumps(document))

        with self.assertRaisesRegex(CheckpointRejectedError, "integrity"):
            self.manager.read_checkpoint(checkpoint["checkpoint_id"])

        self.assertEqual(self.manager.drain_events()[-1]["event"], "rejected")

    def test_malformed_and_incompatible_checkpoints_are_rejected(self) -> None:
        checkpoint = self.manager.create_checkpoint(
            "worker-1", 1234, {"counter": 1, "accumulator": 1}
        )
        path = self.directory / f"{checkpoint['checkpoint_id']}.json"
        original_document = json.loads(path.read_text())
        path.write_text("{not-json")
        with self.assertRaises(CheckpointRejectedError):
            self.manager.read_checkpoint(checkpoint["checkpoint_id"])

        incompatible = original_document
        incompatible["schema_version"] = 99
        path.write_text(json.dumps(incompatible))
        with self.assertRaisesRegex(CheckpointRejectedError, "unsupported"):
            self.manager.read_checkpoint(checkpoint["checkpoint_id"])

    def test_unsupported_or_invalid_application_state_is_rejected(self) -> None:
        for state in (
            {"counter": 1, "accumulator": 2, "pid": 1234},
            {"counter": True, "accumulator": 2},
            {"counter": -1, "accumulator": 2},
            {"counter": 1, "accumulator": "2"},
        ):
            with self.subTest(state=state):
                with self.assertRaises(CheckpointRejectedError):
                    self.manager.create_checkpoint("worker-1", 1234, state)

    def test_budget_evicts_oldest_checkpoint_and_tracks_actual_bytes(self) -> None:
        first = self.manager.create_checkpoint(
            "worker-1", 1234, {"counter": 1, "accumulator": 1}
        )
        self.manager.max_storage_bytes = first["size_bytes"]

        second = self.manager.create_checkpoint(
            "worker-1", 1234, {"counter": 2, "accumulator": 3}
        )

        self.assertLessEqual(
            self.manager.storage_usage_bytes(), self.manager.max_storage_bytes
        )
        with self.assertRaises(CheckpointRejectedError):
            self.manager.read_checkpoint(first["checkpoint_id"])
        self.assertEqual(
            self.manager.read_checkpoint(second["checkpoint_id"])["application_state"],
            {"counter": 2, "accumulator": 3},
        )
        self.assertIn(
            "evicted",
            [event["event"] for event in self.manager.drain_events()],
        )

    def test_checkpoint_larger_than_budget_is_rejected(self) -> None:
        manager = CheckpointManager(self.directory / "tiny", max_storage_bytes=10)
        with self.assertRaises(CheckpointBudgetError):
            manager.create_checkpoint(
                "worker-1", 1234, {"counter": 1, "accumulator": 1}
            )
        self.assertEqual(manager.storage_usage_bytes(), 0)

    def test_worker_identity_is_checked_before_restore(self) -> None:
        checkpoint = self.manager.create_checkpoint(
            "worker-1", 1234, {"counter": 1, "accumulator": 1}
        )
        called = False

        def restore(_state: dict[str, int]) -> None:
            nonlocal called
            called = True

        with self.assertRaisesRegex(CheckpointRejectedError, "different worker"):
            self.manager.restore_checkpoint(
                checkpoint["checkpoint_id"], "worker-2", restore
            )
        self.assertFalse(called)

    def test_active_restore_checkpoint_is_not_evicted(self) -> None:
        first = self.manager.create_checkpoint(
            "worker-1", 1234, {"counter": 1, "accumulator": 1}
        )
        self.manager.max_storage_bytes = first["size_bytes"]

        def restore(_state: dict[str, int]) -> None:
            with self.assertRaises(CheckpointBudgetError):
                self.manager.create_checkpoint(
                    "worker-2", 5678, {"counter": 2, "accumulator": 3}
                )

        self.manager.restore_checkpoint(first["checkpoint_id"], "worker-1", restore)
        self.assertEqual(
            self.manager.read_checkpoint(first["checkpoint_id"])["worker_id"],
            "worker-1",
        )

    def test_atomic_write_failure_leaves_no_partial_checkpoint(self) -> None:
        with mock.patch(
            "twincoreos.checkpoint_manager.os.replace",
            side_effect=OSError("injected replace failure"),
        ):
            with self.assertRaisesRegex(CheckpointError, "write failed"):
                self.manager.create_checkpoint(
                    "worker-1", 1234, {"counter": 1, "accumulator": 1}
                )

        self.assertEqual(list(self.directory.glob("*.json")), [])
        self.assertEqual(list(self.directory.glob(".checkpoint-*.tmp")), [])

    def test_concurrent_checkpoint_requests_have_unique_ids_and_bounded_usage(self) -> None:
        results: list[dict[str, object]] = []
        errors: list[Exception] = []

        def create(index: int) -> None:
            try:
                results.append(
                    self.manager.create_checkpoint(
                        f"worker-{index}",
                        1000 + index,
                        {"counter": index, "accumulator": index * 2},
                    )
                )
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=create, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertFalse(errors)
        self.assertEqual(len({result["checkpoint_id"] for result in results}), 8)
        self.assertLessEqual(
            self.manager.storage_usage_bytes(), self.manager.max_storage_bytes
        )

    def test_real_worker_state_restores_into_fresh_worker_and_continues(self) -> None:
        with WorkerPool(workers=1, memory_mib=1) as original_pool:
            original_pid, advanced_state = original_pool.advance_application_state(
                "worker-1", 5
            )
            self.assertEqual(advanced_state, {"counter": 5, "accumulator": 15})
            checkpoint = self.manager.create_checkpoint(
                "worker-1",
                original_pid,
                advanced_state,
            )

        with WorkerPool(workers=1, memory_mib=1) as replacement_pool:
            replacement_worker = replacement_pool._processes[0]
            self.assertNotEqual(replacement_worker.child.pid, original_pid)
            restored_result = self.manager.restore_checkpoint(
                checkpoint["checkpoint_id"],
                "worker-1",
                lambda state: replacement_pool.restore_application_state(
                    "worker-1", state
                ),
            )
            self.assertEqual(
                restored_result["application_state"],
                {"counter": 5, "accumulator": 15},
            )
            continued_pid, continued_state = (
                replacement_pool.advance_application_state("worker-1", 2)
            )
            self.assertEqual(continued_pid, replacement_worker.child.pid)
            self.assertEqual(continued_state, {"counter": 7, "accumulator": 28})

        self.assertEqual(
            [event["event"] for event in self.manager.drain_events()],
            ["created", "restored"],
        )


if __name__ == "__main__":
    unittest.main()
