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

from twincoreos.api import TwinCoreRequestHandler, create_server
from twincoreos.checkpoint_manager import CheckpointManager
from twincoreos.embedded_telemetry import _checkpoint_integrity_hash
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
            risk_model_path=Path(self.temporary.name) / "no-model.joblib",
            embedded_data_dir=Path(self.temporary.name) / "embedded-data",
            embedded_ml_model_path=Path(self.temporary.name) / "no-embedded-model.joblib",
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

    def test_client_disconnect_does_not_escape_request_handler(self) -> None:
        handler = object.__new__(TwinCoreRequestHandler)
        with patch(
            "twincoreos.api.BaseHTTPRequestHandler.handle",
            side_effect=ConnectionResetError,
        ):
            handler.handle()
        self.assertTrue(handler.close_connection)

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
        deadline = time.monotonic() + 3
        risks = self._get("/api/risk")
        while (
            time.monotonic() < deadline
            and any(
                item["risk_category"] == "WARMING_UP"
                for item in risks["assessments"]
            )
        ):
            time.sleep(0.05)
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
        assessment = risks["assessments"][0]
        self.assertEqual(assessment["type"], "risk_assessment")
        self.assertIn("risk_score", assessment)
        self.assertIn("risk_category", assessment)
        self.assertIn("observed_features", assessment)
        self.assertEqual(
            assessment["observed_features"]["model_type"], "baseline_fallback"
        )
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
            risk_model_path=Path(self.temporary.name) / "no-model.joblib",
            embedded_data_dir=Path(self.temporary.name) / "persisted-embedded-data",
            embedded_ml_model_path=Path(self.temporary.name) / "no-embedded-model.joblib",
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
            risk_model_path=Path(self.temporary.name) / "no-model.joblib",
            embedded_data_dir=Path(self.temporary.name) / "empty-embedded-data",
            embedded_ml_model_path=Path(self.temporary.name) / "no-embedded-model.joblib",
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

        replacement_id = recovery["replacement_worker_id"]
        deadline = time.monotonic() + 4
        dashboard = self._get("/api/dashboard")
        replacement = None
        while time.monotonic() < deadline:
            dashboard = self._get("/api/dashboard")
            replacement = next(
                (
                    worker
                    for worker in dashboard["workers"]
                    if worker["worker_id"] == replacement_id
                ),
                None,
            )
            if (
                replacement is not None
                and replacement["risk"] is not None
                and replacement["risk"]["risk_category"] != "WARMING_UP"
            ):
                break
            time.sleep(0.05)
        self.assertIsNotNone(replacement)
        self.assertEqual(replacement["digital_twin"]["health_state"], "HEALTHY")
        self.assertIsNotNone(replacement["risk"]["risk_score"])

        original = next(
            worker
            for worker in dashboard["workers"]
            if worker["worker_id"] == "worker-1"
        )
        self.assertEqual(original["digital_twin"]["health_state"], "FAILED")
        self.assertEqual(original["risk"]["risk_category"], "HIGH")
        self.assertEqual(original["risk"]["risk_score"], 1.0)
        self.assertEqual(
            original["risk"]["observed_features"]["model_type"],
            "baseline_failure_override",
        )
        lifecycle_events = self._get("/api/events?limit=500")["events"]
        self.assertTrue(
            any(
                event.get("type") == "recovery_event"
                and event.get("event") == "worker_terminated"
                and event.get("worker_id") == "worker-1"
                and event.get("pid") == original_pid
                for event in lifecycle_events
            )
        )
        replacement_risk_history = [
            event
            for event in lifecycle_events
            if event.get("type") == "risk_assessment"
            and event.get("worker_id") == replacement_id
        ]
        self.assertTrue(
            any(
                event["risk_category"] == "WARMING_UP"
                and event["risk_score"] is None
                for event in replacement_risk_history
            )
        )
        self.assertTrue(
            any(
                isinstance(event["risk_score"], (int, float))
                and event["risk_category"] != "WARMING_UP"
                for event in replacement_risk_history
            )
        )
        self.assertTrue(
            any(
                event.get("type") == "risk_assessment"
                and event.get("worker_id") == "worker-1"
                and event.get("risk_score") == 1.0
                and event.get("risk_category") == "HIGH"
                for event in lifecycle_events
            )
        )
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
        shadow = self._get("/api/embedded/ml")
        self.assertEqual(shadow["mode"], "shadow")
        self.assertTrue(shadow["shadow_only"])
        self.assertFalse(shadow["checkpoint_commands_enabled"])
        self.assertEqual(len(shadow["latest_by_task"]), 1)
        self.assertEqual(shadow["latest_by_task"][0]["task_id"], "sensor-sampler")
        self.assertFalse(shadow["latest_by_task"][0]["checkpoint_command_issued"])
        self.assertEqual(
            self._get("/api/dashboard")["embedded_ml"],
            shadow,
        )
        journal = (
            Path(self.temporary.name)
            / "embedded-data"
            / "live-observations.jsonl"
        )
        self.assertEqual(len(journal.read_text(encoding="utf-8").splitlines()), 1)
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
        self.assertEqual(assessment["risk_category"], "LOW")
        self.assertEqual(assessment["risk_index"], 0)
        self.assertEqual(assessment["telemetry_freshness"], "fresh")
        self.assertEqual(assessment["recommendation"], "none")
        self.assertIn("125 words is above", assessment["reason"])
        self.assertIn("32-word threshold", assessment["reason"])
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
        embedded_ids = {
            item["task_id"]
            for item in dashboard["embedded_risk_assessments"]
        }
        self.assertEqual(embedded_ids, {"sensor-sampler", "control-loop"})
        self.assertTrue(
            all(
                not {"pid", "cpu_utilization_percent_one_core", "memory_rss_bytes"}
                & item["observed_inputs"].keys()
                for item in dashboard["embedded_risk_assessments"]
            )
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
                and item["risk_category"] == "UNKNOWN"
                and item["risk_index"] is None
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
        control_record = {
            **record,
            "task_id": "control-loop",
            "stack_high_water_mark_words": 125,
        }
        status, result = self._post(
            "/api/embedded/telemetry",
            {
                "bridge_session_id": bridge_session_id,
                "telemetry": control_record,
            },
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
        self.assertEqual(by_task_id["sensor-sampler"]["risk_level"], "unknown")
        self.assertEqual(by_task_id["sensor-sampler"]["recommendation"], "none")
        self.assertEqual(by_task_id["sensor-sampler"]["matched_rules"], ["telemetry_stale"])
        self.assertEqual(by_task_id["control-loop"]["risk_level"], "normal")
        self.assertEqual(by_task_id["control-loop"]["risk_category"], "LOW")
        self.assertNotIn("worker-1", by_task_id)
        status, rejected = self._post(
            "/api/embedded/fault-injection",
            {"task_id": "sensor-sampler", "confirm": True},
        )
        self.assertEqual(status, 409)
        self.assertFalse(rejected["success"])

    def test_embedded_fault_request_uses_fresh_task_telemetry_and_uart_command(self) -> None:
        status, rejected = self._post(
            "/api/embedded/fault-injection",
            {"task_id": "sensor-sampler", "confirm": True},
        )
        self.assertEqual(status, 409)
        self.assertFalse(rejected["success"])

        self.runtime._embedded_risk_policy.minimum_stack_high_water_words = 96
        session_id = "97897e63-8662-4a26-9e13-43912a7db891"
        for task_id in ("sensor-sampler", "control-loop"):
            telemetry = {
                "schema_version": 1,
                "message_type": "task_telemetry",
                "target": "freertos-qemu-rv32",
                "task_id": task_id,
                "task_state": "running",
                "tick_count": 1000,
                "execution_count": 3,
                "stack_high_water_mark_words": 125,
            }
            self.assertEqual(
                self._post(
                    "/api/embedded/telemetry",
                    {"bridge_session_id": session_id, "telemetry": telemetry},
                )[0],
                200,
            )

        status, accepted = self._post(
            "/api/embedded/fault-injection",
            {"task_id": "sensor-sampler", "confirm": True},
        )
        self.assertEqual(status, 202)
        self.assertTrue(accepted["success"])
        self.assertEqual(accepted["request"]["status"], "queued_for_uart")
        run_id = accepted["request"]["run_id"]
        self.assertEqual(accepted["request"]["request_id"], run_id)

        command_path = (
            "/api/embedded/bridge/command?bridge_session_id=" + session_id
        )
        dispatched = self._get(command_path)
        self.assertEqual(
            dispatched["command"]["command"],
            f"INJECT_STACK_PRESSURE sensor-sampler {run_id}",
        )
        self.assertEqual(dispatched["command"]["run_id"], run_id)
        self.assertIsNone(self._get(command_path)["command"])
        workflow = self._get("/api/dashboard")["embedded_fault_injection"]
        self.assertEqual(workflow["status"], "uart_command_dispatched")
        self.assertEqual(workflow["checkpoint_id"], run_id)

        overlap_status, overlap = self._post(
            "/api/embedded/fault-injection",
            {"task_id": "sensor-sampler", "confirm": True},
        )
        self.assertEqual(overlap_status, 409)
        self.assertFalse(overlap["success"])

        stale_telemetry = {
            "schema_version": 1,
            "message_type": "task_telemetry",
            "target": "freertos-qemu-rv32",
            "task_id": "sensor-sampler",
            "task_state": "running",
            "tick_count": 1250,
            "execution_count": 4,
            "stack_high_water_mark_words": 125,
        }
        status, stale_response = self._post(
            "/api/embedded/telemetry",
            {
                "bridge_session_id": session_id,
                "telemetry": stale_telemetry,
            },
        )
        self.assertEqual(status, 200)
        self.assertFalse(stale_response["accepted"])
        self.assertTrue(stale_response["stale"])

        stale_event = {
            "schema_version": 1,
            "message_type": "task_event",
            "target": "freertos-qemu-rv32",
            "task_id": "sensor-sampler",
            "event": "fault_injection_requested",
            "run_id": "b5534e8f-bc8f-4b44-9bec-c69bbd9ee7ee",
        }
        status, stale_ack = self._post(
            "/api/embedded/events",
            {"bridge_session_id": session_id, "event": stale_event},
        )
        self.assertEqual(status, 200)
        self.assertFalse(stale_ack["accepted"])
        self.assertTrue(stale_ack["stale"])

        matching_event = {**stale_event, "run_id": run_id}
        status, matching_ack = self._post(
            "/api/embedded/events",
            {"bridge_session_id": session_id, "event": matching_event},
        )
        self.assertEqual(status, 200)
        self.assertTrue(matching_ack["success"])
        self.assertEqual(matching_ack["event"]["event"], "fault_injection_requested")
        stressed_telemetry = {
            "schema_version": 1,
            "message_type": "task_telemetry",
            "target": "freertos-qemu-rv32",
            "task_id": "sensor-sampler",
            "task_state": "running",
            "tick_count": 1250,
            "execution_count": 4,
            "stack_high_water_mark_words": 95,
            "run_id": run_id,
        }
        status, stressed_ack = self._post(
            "/api/embedded/telemetry",
            {
                "bridge_session_id": session_id,
                "telemetry": stressed_telemetry,
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(stressed_ack["success"])
        self.assertEqual(stressed_ack["task"]["stack_high_water_mark_words"], 95)
        workflow = self._get("/api/dashboard")["embedded_fault_injection"]
        self.assertEqual(workflow["status"], "elevated_risk_observed")
        self.assertEqual(
            workflow["elevated_assessment"]["observed_inputs"][
                "stack_high_water_mark_words"
            ],
            95,
        )
        self.assertEqual(
            workflow["elevated_assessment"]["policy_configuration"][
                "minimum_stack_high_water_words"
            ],
            96,
        )

        failure_event = {
            "schema_version": 1,
            "message_type": "task_event",
            "target": "freertos-qemu-rv32",
            "task_id": "sensor-sampler",
            "event": "recovery_failed",
            "run_id": run_id,
        }
        status, failure_ack = self._post(
            "/api/embedded/events",
            {"bridge_session_id": session_id, "event": failure_event},
        )
        self.assertEqual(status, 200)
        self.assertTrue(failure_ack["success"])
        self.assertEqual(failure_ack["event"]["event"], "recovery_failed")
        same_session_status, same_session_retry = self._post(
            "/api/embedded/fault-injection",
            {"task_id": "sensor-sampler", "confirm": True},
        )
        self.assertEqual(same_session_status, 409)
        self.assertIn("restart the bridge", same_session_retry["error"])

        restarted_session_id = "e219b7fd-01a5-4c33-9ef7-69ea15bca24f"
        for task_id in ("sensor-sampler", "control-loop"):
            restarted_telemetry = {
                "schema_version": 1,
                "message_type": "task_telemetry",
                "target": "freertos-qemu-rv32",
                "task_id": task_id,
                "task_state": "running",
                "tick_count": 2000,
                "execution_count": 1,
                "stack_high_water_mark_words": 125,
            }
            self.assertEqual(
                self._post(
                    "/api/embedded/telemetry",
                    {
                        "bridge_session_id": restarted_session_id,
                        "telemetry": restarted_telemetry,
                    },
                )[0],
                200,
            )
        retry_status, retry = self._post(
            "/api/embedded/fault-injection",
            {"task_id": "sensor-sampler", "confirm": True},
        )
        self.assertEqual(retry_status, 202)
        self.assertTrue(retry["success"])
        self.assertNotEqual(retry["request"]["run_id"], run_id)

        unsupported_status, unsupported = self._post(
            "/api/embedded/fault-injection",
            {"task_id": "control-loop", "confirm": True},
        )
        self.assertEqual(unsupported_status, 409)
        self.assertFalse(unsupported["success"])

    def test_embedded_fault_request_rejects_demo_threshold_below_measured_trigger(self) -> None:
        session_id = "97897e63-8662-4a26-9e13-43912a7db891"
        for task_id in ("sensor-sampler", "control-loop"):
            telemetry = {
                "schema_version": 1,
                "message_type": "task_telemetry",
                "target": "freertos-qemu-rv32",
                "task_id": task_id,
                "task_state": "running",
                "tick_count": 1000,
                "execution_count": 3,
                "stack_high_water_mark_words": 125,
            }
            self.assertEqual(
                self._post(
                    "/api/embedded/telemetry",
                    {"bridge_session_id": session_id, "telemetry": telemetry},
                )[0],
                200,
            )

        status, rejected = self._post(
            "/api/embedded/fault-injection",
            {"task_id": "sensor-sampler", "confirm": True},
        )
        self.assertEqual(status, 409)
        self.assertFalse(rejected["success"])
        self.assertIn("requires", rejected["error"])
        self.assertIn("--embedded-stack-reserve-threshold-words 96", rejected["error"])
        self.assertIsNone(self._get("/api/dashboard")["embedded_fault_injection"])

    def test_embedded_fault_request_does_not_repurpose_host_recovery_endpoint(self) -> None:
        self._wait_for_sample()
        status, response = self._post(
            "/api/workers/worker-1/fault", {"confirm": True}
        )
        self.assertEqual(status, 200)
        self.assertTrue(response["success"])
        recovery = response["recovery"]
        self.assertIn("replacement_pid", recovery)
        self.assertEqual(
            self._get("/api/dashboard")["embedded_fault_injection"], None
        )

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

    def test_embedded_risk_transitions_are_retained_with_measured_inputs(self) -> None:
        self._wait_for_sample()
        session_id = "356b9bc2-e9a0-4fc4-a9af-1c4a0b1e798e"

        def ingest(task_id: str, execution: int, reserve: int) -> None:
            telemetry = {
                "schema_version": 1,
                "message_type": "task_telemetry",
                "target": "freertos-qemu-rv32",
                "task_id": task_id,
                "task_state": "running",
                "tick_count": execution * 250,
                "execution_count": execution,
                "stack_high_water_mark_words": reserve,
            }
            status, response = self._post(
                "/api/embedded/telemetry",
                {"bridge_session_id": session_id, "telemetry": telemetry},
            )
            self.assertEqual(status, 200)
            self.assertTrue(response["success"])

        ingest("sensor-sampler", 1, 64)
        ingest("control-loop", 1, 64)
        ingest("sensor-sampler", 2, 32)
        ingest("sensor-sampler", 3, 64)

        transitions = self._get("/api/dashboard")["embedded_risk_events"]
        sensor_events = [
            item for item in transitions if item["task_id"] == "sensor-sampler"
        ]
        self.assertEqual(
            [(item["previous_category"], item["risk_category"]) for item in sensor_events],
            [("UNKNOWN", "LOW"), ("LOW", "ELEVATED"), ("ELEVATED", "LOW")],
        )
        elevated = sensor_events[1]
        self.assertEqual(elevated["stack_reserve_words"], 32)
        self.assertEqual(elevated["threshold_words"], 32)
        self.assertEqual(elevated["risk_index"], 100)
        self.assertEqual(elevated["execution_count"], 2)
        self.assertTrue(elevated["occurred_at_utc"])
        current = {
            item["task_id"]: item
            for item in self._get("/api/embedded/risk")["assessments"]
        }
        self.assertEqual(current["sensor-sampler"]["risk_category"], "LOW")
        self.assertEqual(current["sensor-sampler"]["risk_index"], 0)
        self.assertEqual(current["control-loop"]["risk_category"], "LOW")
        self.assertEqual(
            [item["risk_category"] for item in transitions if item["task_id"] == "control-loop"],
            ["LOW"],
        )
        with self.runtime._guard:
            self.runtime._embedded_latest[
                ("freertos-qemu-rv32", "sensor-sampler")
            ]["received_at_utc"] = "2000-01-01T00:00:00+00:00"
        stale_snapshot = self._get("/api/dashboard")
        sensor_transitions = [
            item
            for item in stale_snapshot["embedded_risk_events"]
            if item["task_id"] == "sensor-sampler"
        ]
        self.assertEqual(sensor_transitions[-1]["previous_category"], "LOW")
        self.assertEqual(sensor_transitions[-1]["risk_category"], "UNKNOWN")
        self.assertIsNone(sensor_transitions[-1]["risk_index"])
        stale_current = {
            item["task_id"]: item
            for item in stale_snapshot["embedded_risk_assessments"]
        }
        self.assertEqual(stale_current["sensor-sampler"]["risk_category"], "UNKNOWN")
        self.assertEqual(stale_current["control-loop"]["risk_category"], "LOW")

    def test_embedded_checkpoint_events_are_validated_and_recorded_separately(self) -> None:
        self._wait_for_sample()
        session_id = "da973c6f-a8ce-4e41-8d14-6b965bc9a552"
        telemetry = {
            "schema_version": 1,
            "message_type": "task_telemetry",
            "target": "freertos-qemu-rv32",
            "task_id": "sensor-sampler",
            "task_state": "running",
            "tick_count": 250,
            "execution_count": 1,
            "stack_high_water_mark_words": 200,
        }
        self.assertEqual(
            self._post(
                "/api/embedded/telemetry",
                {"bridge_session_id": session_id, "telemetry": telemetry},
            )[0],
            200,
        )
        event = {
            "schema_version": 1,
            "message_type": "task_event",
            "target": "freertos-qemu-rv32",
            "task_id": "sensor-sampler",
            "event": "checkpoint_saved",
            "run_id": "b5534e8f-bc8f-4b44-9bec-c69bbd9ee7ee",
            "counter": 4,
            "accumulator": 2089706401,
            "integrity_hash": 3568767710,
            "checkpoint_size_bytes": 24,
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
        embedded_storage = dashboard["embedded_checkpoint_storage"]
        self.assertEqual(embedded_storage["checkpoint_id"], event["run_id"])
        self.assertEqual(embedded_storage["size_bytes"], 24)
        self.assertEqual(embedded_storage["storage_used_bytes"], 24)
        self.assertIsNone(embedded_storage["storage_budget_bytes"])
        self.assertEqual(
            embedded_storage["application_state"]["accumulator"],
            event["accumulator"],
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

        new_session_id = "5ba7641e-5c1c-45c3-a0d2-e02cf65a3136"
        self.assertEqual(
            self._post(
                "/api/embedded/telemetry",
                {
                    "bridge_session_id": new_session_id,
                    "telemetry": telemetry,
                },
            )[0],
            200,
        )
        restarted_dashboard = self._get("/api/dashboard")
        self.assertIsNone(restarted_dashboard["embedded_checkpoint_storage"])
        self.assertEqual(restarted_dashboard["embedded_checkpoint_status"], [])
        self.assertEqual(
            [task["task_id"] for task in restarted_dashboard["embedded_tasks"]["latest"]],
            ["sensor-sampler"],
        )
        restarted_assessments = {
            item["task_id"]: item
            for item in restarted_dashboard["embedded_risk_assessments"]
        }
        self.assertEqual(restarted_assessments["control-loop"]["risk_category"], "UNKNOWN")
        self.assertTrue(
            any(
                item["type"] == "embedded_task_event"
                and item["event"] == "checkpoint_saved"
                for item in restarted_dashboard["events"]["events"]
            )
        )

        invalid = {**event, "task_id": "worker-1"}
        status, failure = self._post(
            "/api/embedded/events",
            {"bridge_session_id": session_id, "event": invalid},
        )
        self.assertEqual(status, 400)
        self.assertFalse(failure["success"])

    def test_control_loop_risk_is_embedded_but_checkpoint_is_not_enabled(self) -> None:
        session_id = "5cd7e756-4dbe-47b8-8ed7-c8dd464c9088"
        telemetry = {
            "schema_version": 1,
            "message_type": "task_telemetry",
            "target": "freertos-qemu-rv32",
            "task_id": "control-loop",
            "task_state": "running",
            "tick_count": 1000,
            "execution_count": 2,
            "stack_high_water_mark_words": 16,
        }
        status, accepted = self._post(
            "/api/embedded/telemetry",
            {"bridge_session_id": session_id, "telemetry": telemetry},
        )
        self.assertEqual(status, 200)
        self.assertFalse(accepted["duplicate"])

        risk = self._get("/api/embedded/risk")["assessments"]
        control_assessment = next(
            item for item in risk if item["task_id"] == "control-loop"
        )
        self.assertEqual(control_assessment["risk_level"], "elevated")
        self.assertEqual(control_assessment["risk_category"], "ELEVATED")
        self.assertEqual(
            control_assessment["recommendation"], "checkpoint_unsupported"
        )
        self.assertFalse(control_assessment["checkpoint_supported"])
        self.assertEqual(
            control_assessment["observed_inputs"]["stack_high_water_mark_words"],
            16,
        )

        checkpoint_event = {
            "schema_version": 1,
            "message_type": "task_event",
            "target": "freertos-qemu-rv32",
            "task_id": "control-loop",
            "event": "checkpoint_saved",
            "counter": 2,
            "accumulator": 123456,
            "integrity_hash": 0,
            "checkpoint_size_bytes": 24,
        }
        status, event_result = self._post(
            "/api/embedded/events",
            {"bridge_session_id": session_id, "event": checkpoint_event},
        )
        self.assertEqual(status, 400)
        self.assertFalse(event_result["success"])

        dashboard = self._get("/api/dashboard")
        self.assertEqual(dashboard["embedded_checkpoint_status"], [])
        self.assertTrue(
            any(
                item["task_id"] == "control-loop"
                and item["execution_count"] == 2
                for item in dashboard["embedded_tasks"]["latest"]
            )
        )

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
