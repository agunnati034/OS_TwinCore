from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from twincoreos.api import create_server
from twincoreos.checkpoint_manager import CheckpointManager
from twincoreos.runtime import MonitorRuntime


class ApiIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.runtime = MonitorRuntime(
            workers=2,
            memory_mib=1,
            sample_interval=0.2,
            checkpoint_interval=0.4,
            checkpoint_dir=Path(self.temporary.name) / "checkpoints",
        )
        self.runtime.start()
        self.server = create_server(self.runtime, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.runtime.stop()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def _get(self, path: str) -> dict[str, object]:
        with urlopen(self.base_url + path, timeout=5) as response:
            return json.loads(response.read())

    def _post(self, path: str, body: dict[str, object]) -> tuple[int, dict[str, object]]:
        request = Request(
            self.base_url + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            try:
                return error.code, json.loads(error.read())
            finally:
                error.close()

    def _wait_for_sample(self) -> dict[str, object]:
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            response = self._get("/api/workers")
            workers = response["workers"]
            if workers and all(worker["telemetry"] is not None for worker in workers):
                return response
            time.sleep(0.05)
        self.fail("backend did not publish real worker samples")

    def test_endpoints_publish_real_worker_and_system_data(self) -> None:
        self._wait_for_sample()
        original_pids = {
            worker["telemetry"]["pid"]
            for worker in self._get("/api/workers")["workers"]
        }
        self.runtime.start()
        self.assertEqual(
            {
                worker["telemetry"]["pid"]
                for worker in self._get("/api/workers")["workers"]
            },
            original_pids,
        )
        status = self._get("/api/status")
        workers = self._get("/api/workers")["workers"]
        telemetry = self._get("/api/telemetry")
        twins = self._get("/api/twins")
        risks = self._get("/api/risk")
        checkpoints = self._get("/api/checkpoints")
        events = self._get("/api/events?limit=20")
        recovery = self._get("/api/recovery")

        self.assertEqual(status["status"], "running")
        self.assertEqual(len(workers), 2)
        self.assertTrue(all(worker["telemetry"]["pid"] > 0 for worker in workers))
        self.assertEqual(len(telemetry["latest"]), 2)
        self.assertEqual(len(twins["twins"]), 2)
        self.assertEqual(len(risks["assessments"]), 2)
        self.assertIn("storage_usage_bytes", checkpoints["storage"])
        self.assertTrue(any(event["type"] == "telemetry" for event in events["events"]))
        self.assertIsNone(recovery["recovery"])
        dashboard = self._get("/api/dashboard")
        self.assertEqual(dashboard["status"]["status"], "running")
        self.assertEqual(len(dashboard["workers"]), 2)

    def test_unchanged_application_state_does_not_create_duplicate_checkpoints(self) -> None:
        self._wait_for_sample()
        time.sleep(0.65)
        inventory = self._get("/api/checkpoints")
        self.assertLessEqual(len(inventory["checkpoints"]), 2)

    def test_existing_checkpoint_is_reused_for_unchanged_worker_state(self) -> None:
        directory = Path(self.temporary.name) / "persisted"
        manager = CheckpointManager(directory, 100_000)
        manager.create_checkpoint(
            "worker-1",
            12345,
            {"counter": 0, "accumulator": 0},
        )
        self.runtime.stop()
        runtime = MonitorRuntime(
            workers=1,
            memory_mib=1,
            sample_interval=0.1,
            checkpoint_interval=0.2,
            checkpoint_dir=directory,
        )
        try:
            runtime.start()
            time.sleep(0.35)
            worker_checkpoints = [
                checkpoint
                for checkpoint in runtime.snapshot()["checkpoints"]
                if checkpoint["worker_id"] == "worker-1"
            ]
            self.assertEqual(len(worker_checkpoints), 1)
        finally:
            runtime.stop()


    def test_telemetry_is_explicitly_empty_before_first_sample(self) -> None:
        self.runtime.stop()
        replacement = MonitorRuntime(
            workers=1,
            memory_mib=1,
            sample_interval=0.8,
            checkpoint_interval=1.0,
            checkpoint_dir=Path(self.temporary.name) / "empty-checkpoints",
        )
        old_server = self.server
        old_server.runtime = replacement
        self.runtime = replacement
        replacement.start()
        response = self._get("/api/telemetry")
        self.assertEqual(response["latest"], [])
        self.assertEqual(response["history"], [])
        replacement.stop()

    def test_invalid_worker_id_and_confirmation_are_rejected(self) -> None:
        self._wait_for_sample()
        status, response = self._post(
            "/api/workers/not-a-worker/fault", {"confirm": True}
        )
        self.assertEqual(status, 404)
        self.assertFalse(response["success"])

        status, response = self._post(
            "/api/workers/worker-1/fault", {"confirm": False}
        )
        self.assertEqual(status, 400)
        self.assertFalse(response["success"])

    def test_fault_endpoint_returns_verified_recovery_outcome(self) -> None:
        self._wait_for_sample()
        original_workers = self._get("/api/workers")["workers"]
        original_pid = next(
            worker["telemetry"]["pid"]
            for worker in original_workers
            if worker["worker_id"] == "worker-1"
        )
        status, response = self._post(
            "/api/workers/worker-1/fault", {"confirm": True}
        )
        self.assertEqual(status, 200)
        recovery = response["recovery"]
        self.assertTrue(response["success"])
        self.assertTrue(recovery["success"])
        self.assertEqual(recovery["failed_pid"], original_pid)
        self.assertNotEqual(recovery["replacement_pid"], original_pid)
        self.assertEqual(
            recovery["continued_state"]["counter"],
            recovery["restored_state"]["counter"] + 1,
        )
        self.assertEqual(
            [event["event"] for event in recovery["events"]],
            [
                "fault_requested",
                "worker_terminated",
                "recovery_started",
                "checkpoint_selected",
                "restore_succeeded",
                "recovery_completed",
            ],
        )
        event_types = [
            event["event"] for event in self._get("/api/events?limit=50")["events"]
            if event.get("type") == "recovery_event"
        ]
        self.assertIn("worker_terminated", event_types)
        self.assertIn("restore_succeeded", event_types)
        self.assertIn("recovery_completed", event_types)
        status, response = self._post(
            "/api/workers/worker-1/fault", {"confirm": True}
        )
        self.assertEqual(status, 404)
        self.assertFalse(response["success"])

    def test_fault_error_is_not_reported_as_success(self) -> None:
        self._wait_for_sample()
        with patch.object(
            self.runtime._pool,
            "inject_termination",
            side_effect=RuntimeError("termination failed"),
        ):
            status, response = self._post(
                "/api/workers/worker-1/fault", {"confirm": True}
            )
        self.assertEqual(status, 409)
        self.assertFalse(response["success"])
        self.assertIn("termination failed", response["error"])
        self.assertEqual(
            [event["event"] for event in response["events"]],
            ["fault_requested", "recovery_failed"],
        )

    def test_events_limit_is_validated(self) -> None:
        status = None
        try:
            self._get("/api/events?limit=501")
        except HTTPError as error:
            status = error.code
            error.close()
        self.assertEqual(status, 400)

    def test_cors_allows_only_the_local_dashboard_origin(self) -> None:
        allowed = Request(
            self.base_url + "/api/status",
            headers={"Origin": "http://127.0.0.1:5173"},
        )
        with urlopen(allowed, timeout=5) as response:
            self.assertEqual(
                response.headers.get("Access-Control-Allow-Origin"),
                "http://127.0.0.1:5173",
            )
        disallowed = Request(
            self.base_url,
            headers={"Origin": "https://untrusted.example"},
            method="OPTIONS",
        )
        try:
            urlopen(disallowed, timeout=5)
        except HTTPError as error:
            self.assertEqual(error.code, 403)
            error.close()
        else:
            self.fail("untrusted browser origin should be rejected")

    def test_embedded_telemetry_is_ingested_separately_and_deduplicated(self) -> None:
        self._wait_for_sample()
        session_id = "0d179e32-3fbc-4ee1-830b-b69256131d89"
        first = {
            "schema_version": 1,
            "message_type": "task_telemetry",
            "target": "freertos-qemu-rv32",
            "task_id": "sensor-sampler",
            "task_state": "running",
            "tick_count": 250,
            "execution_count": 2,
            "stack_high_water_mark_words": 125,
        }
        status, response = self._post(
            "/api/embedded/telemetry",
            {"bridge_session_id": session_id, "telemetry": first},
        )
        self.assertEqual(status, 200)
        self.assertTrue(response["success"])
        self.assertFalse(response["duplicate"])

        status, duplicate = self._post(
            "/api/embedded/telemetry",
            {"bridge_session_id": session_id, "telemetry": first},
        )
        self.assertEqual(status, 200)
        self.assertTrue(duplicate["duplicate"])

        embedded = self._get("/api/embedded/tasks")
        self.assertEqual(len(embedded["latest"]), 1)
        task = embedded["latest"][0]
        self.assertEqual(task["task_id"], "sensor-sampler")
        self.assertEqual(task["execution_count"], 2)
        self.assertEqual(task["stack_high_water_mark_words"], 125)
        self.assertNotIn("pid", task)
        self.assertNotIn("memory_rss_bytes", task)
        self.assertNotIn("cpu_utilization_percent_one_core", task)
        self.assertEqual(len(embedded["history"]), 1)
        embedded_risk = self._get("/api/embedded/risk")
        self.assertEqual(
            embedded_risk["assessment_type"], "heuristic_operational_risk"
        )
        assessment = next(
            item
            for item in embedded_risk["assessments"]
            if item["task_id"] == "sensor-sampler"
        )
        self.assertEqual(assessment["risk_level"], "normal")
        self.assertEqual(assessment["recommendation"], "none")
        self.assertNotIn("pid", assessment["observed_inputs"])
        self.assertEqual(
            {item["task_id"] for item in embedded_risk["assessments"]},
            {"sensor-sampler", "control-loop"},
        )

        dashboard = self._get("/api/dashboard")
        self.assertEqual(dashboard["embedded_tasks"]["latest"], embedded["latest"])
        dashboard_assessments = {
            item["task_id"]: item for item in dashboard["embedded_risk_assessments"]
        }
        self.assertEqual(
            dashboard_assessments["sensor-sampler"]["risk_level"], "normal"
        )
        self.assertEqual(
            dashboard_assessments["sensor-sampler"]["recommendation"], "none"
        )
        self.assertEqual(
            {worker["worker_id"] for worker in dashboard["workers"]},
            {"worker-1", "worker-2"},
        )
        event_records = [
            event
            for event in self._get("/api/events?limit=50")["events"]
            if event["type"] == "embedded_task_telemetry"
        ]
        self.assertEqual(len(event_records), 1)

    def test_embedded_risk_endpoint_fails_closed_for_missing_stale_and_unknown_tasks(
        self,
    ) -> None:
        initial = self._get("/api/embedded/risk")
        self.assertEqual(
            {item["task_id"] for item in initial["assessments"]},
            {"sensor-sampler", "control-loop"},
        )
        self.assertTrue(
            all(
                item["risk_level"] == "unknown"
                and item["recommendation"] == "none"
                for item in initial["assessments"]
            )
        )

        bridge_session_id = "504bdeab-a291-4938-8371-825bab48177f"
        record = {
            "schema_version": 1,
            "message_type": "task_telemetry",
            "target": "freertos-qemu-rv32",
            "task_id": "sensor-sampler",
            "task_state": "running",
            "tick_count": 750,
            "execution_count": 3,
            "stack_high_water_mark_words": 16,
        }
        status, result = self._post(
            "/api/embedded/telemetry",
            {"bridge_session_id": bridge_session_id, "telemetry": record},
        )
        self.assertEqual(status, 200)
        self.assertTrue(result["success"])

        with self.runtime._guard:
            self.runtime._embedded_latest[
                ("freertos-qemu-rv32", "sensor-sampler")
            ]["received_at_utc"] = "2000-01-01T00:00:00+00:00"

        status, result = self._post(
            "/api/embedded/telemetry",
            {
                "bridge_session_id": "0d2b86d3-022f-4fe7-a69f-12de48c1a233",
                "telemetry": {**record, "task_id": "worker-1"},
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(result["success"])

        risk = self._get("/api/embedded/risk")
        self.assertEqual(
            {item["task_id"] for item in risk["assessments"]},
            {"sensor-sampler", "control-loop"},
        )
        by_task_id = {item["task_id"]: item for item in risk["assessments"]}
        for task_id in ("sensor-sampler", "control-loop"):
            self.assertEqual(by_task_id[task_id]["risk_level"], "unknown")
            self.assertEqual(by_task_id[task_id]["recommendation"], "none")
        self.assertEqual(by_task_id["sensor-sampler"]["matched_rules"], ["telemetry_stale"])
        self.assertNotIn("worker-1", by_task_id)

    def test_embedded_ingestion_rejects_out_of_order_task_execution(self) -> None:
        self._wait_for_sample()
        session_id = "3bdc5b97-7440-4fc7-a7fb-7bb4d7758f93"
        record = {
            "schema_version": 1,
            "message_type": "task_telemetry",
            "target": "freertos-qemu-rv32",
            "task_id": "sensor-sampler",
            "task_state": "running",
            "tick_count": 500,
            "execution_count": 4,
            "stack_high_water_mark_words": 28,
        }
        self.assertEqual(
            self._post(
                "/api/embedded/telemetry",
                {"bridge_session_id": session_id, "telemetry": record},
            )[0],
            200,
        )
        status, response = self._post(
            "/api/embedded/telemetry",
            {
                "bridge_session_id": session_id,
                "telemetry": {
                    **record,
                    "tick_count": 250,
                    "execution_count": 2,
                },
            },
        )
        self.assertEqual(status, 409)
        self.assertFalse(response["success"])

    def test_embedded_checkpoint_events_are_validated_and_recorded_separately(self) -> None:
        self._wait_for_sample()
        session_id = "da973c6f-a8ce-4e41-8d14-6b965bc9a552"
        event = {
            "schema_version": 1,
            "message_type": "task_event",
            "target": "freertos-qemu-rv32",
            "task_id": "sensor-sampler",
            "event": "checkpoint_saved",
            "counter": 4,
            "accumulator": 2089706401,
            "integrity_hash": 3568767710,
        }
        status, response = self._post(
            "/api/embedded/events",
            {"bridge_session_id": session_id, "event": event},
        )
        self.assertEqual(status, 200)
        self.assertTrue(response["success"])
        accepted_events = [
            item
            for item in self._get("/api/events?limit=100")["events"]
            if item["type"] == "embedded_task_event"
        ]
        self.assertEqual(len(accepted_events), 1)
        self.assertEqual(accepted_events[0]["task_id"], "sensor-sampler")
        self.assertEqual(accepted_events[0]["event"], "checkpoint_saved")
        self.assertNotIn("worker_pid", accepted_events[0])
        dashboard = self._get("/api/dashboard")
        self.assertEqual(
            dashboard["embedded_checkpoint_status"][0]["event"],
            "checkpoint_saved",
        )

        status, failure = self._post(
            "/api/embedded/events",
            {
                "bridge_session_id": session_id,
                "event": {**event, "integrity_hash": event["integrity_hash"] ^ 1},
            },
        )
        self.assertEqual(status, 400)
        self.assertFalse(failure["success"])
        self.assertEqual(
            self._get("/api/dashboard")["embedded_checkpoint_status"][0][
                "integrity_hash"
            ],
            event["integrity_hash"],
        )

        invalid = {**event, "task_id": "worker-1"}
        status, failure = self._post(
            "/api/embedded/events",
            {"bridge_session_id": session_id, "event": invalid},
        )
        self.assertEqual(status, 400)
        self.assertFalse(failure["success"])

    def test_embedded_ingestion_rejects_invalid_and_conflicting_samples(self) -> None:
        self._wait_for_sample()
        session_id = "b7fc7db8-a31f-4a10-aaf2-a891ca782972"
        record = {
            "schema_version": 1,
            "message_type": "task_telemetry",
            "target": "freertos-qemu-rv32",
            "task_id": "control-loop",
            "task_state": "running",
            "tick_count": 1000,
            "execution_count": 1,
            "stack_high_water_mark_words": 158,
        }
        status, invalid = self._post(
            "/api/embedded/telemetry",
            {"bridge_session_id": "not-a-uuid", "telemetry": record},
        )
        self.assertEqual(status, 400)
        self.assertFalse(invalid["success"])
        self.assertEqual(self._get("/api/embedded/tasks")["latest"], [])

        payload = {"bridge_session_id": session_id, "telemetry": record}
        self.assertEqual(self._post("/api/embedded/telemetry", payload)[0], 200)
        changed = {**record, "tick_count": 1001}
        status, conflict = self._post(
            "/api/embedded/telemetry",
            {"bridge_session_id": session_id, "telemetry": changed},
        )
        self.assertEqual(status, 409)
        self.assertFalse(conflict["success"])
        self.assertEqual(len(self._get("/api/embedded/tasks")["history"]), 1)

    def test_embedded_ingestion_reports_unavailable_runtime(self) -> None:
        self.runtime.stop()
        status, response = self._post(
            "/api/embedded/telemetry",
            {
                "bridge_session_id": "b7fc7db8-a31f-4a10-aaf2-a891ca782972",
                "telemetry": {
                    "schema_version": 1,
                    "message_type": "task_telemetry",
                    "target": "freertos-qemu-rv32",
                    "task_id": "sensor-sampler",
                    "task_state": "running",
                    "tick_count": 250,
                    "execution_count": 2,
                    "stack_high_water_mark_words": 125,
                },
            },
        )
        self.assertEqual(status, 503)
        self.assertFalse(response["success"])


if __name__ == "__main__":
    unittest.main()
