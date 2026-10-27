from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from urllib.request import Request, urlopen

from twincoreos.api import create_server
from twincoreos.embedded_telemetry import run_bridge
from twincoreos.runtime import MonitorRuntime


ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = (
    ROOT
    / ".deps"
    / "freertos"
    / "FreeRTOS"
    / "Demo"
    / "RISC-V_RV32_QEMU_VIRT_GCC"
    / "build"
    / "gcc"
    / "output"
    / "RTOSDemo.elf"
)


def checkpoint_hash(counter: int, accumulator: int, task_id: str) -> int:
    result = 2166136261
    numeric_task_id = {"sensor-sampler": 1, "control-loop": 2}[task_id]
    for value in (0x54434F53, 1, numeric_task_id, counter, accumulator):
        for byte in value.to_bytes(4, "little"):
            result = ((result ^ byte) * 16777619) & 0xFFFFFFFF
    return result


@unittest.skipUnless(
    shutil.which(os.environ.get("QEMU", "qemu-system-riscv32")),
    "qemu-system-riscv32 is unavailable",
)
class EmbeddedRiskCheckpointQemuTests(unittest.TestCase):
    def test_uart_stack_fault_changes_risk_and_recovers_sensor_task(self) -> None:
        if not FIRMWARE.is_file():
            self.skipTest("Build firmware first with `make -C embedded build`")

        temporary = tempfile.TemporaryDirectory()
        runtime = MonitorRuntime(
            workers=2,
            memory_mib=1,
            sample_interval=0.2,
            checkpoint_interval=0.4,
            embedded_stack_reserve_threshold_words=96,
            checkpoint_dir=Path(temporary.name) / "checkpoints",
            embedded_data_dir=Path(temporary.name) / "embedded-data",
            embedded_ml_model_path=Path(temporary.name) / "no-embedded-model.joblib",
        )
        runtime.start()
        server = create_server(runtime, port=0)
        server.timeout = 0.1
        server_thread = threading.Thread(
            target=server.serve_forever, daemon=True
        )
        server_thread.start()
        stop = threading.Event()
        bridge_result: list[int] = []
        bridge_error: list[BaseException] = []

        def run() -> None:
            try:
                bridge_result.append(
                    run_bridge(
                        FIRMWARE,
                        f"http://127.0.0.1:{server.server_address[1]}/api/embedded/telemetry",
                        stop_requested=stop.is_set,
                    )
                )
            except BaseException as error:
                bridge_error.append(error)

        bridge_thread = threading.Thread(target=run, daemon=True)
        bridge_thread.start()
        live_assessments: list[dict[str, object]] = []
        initially_normal_tasks: set[str] = set()
        initial_stack_reserves: dict[str, int] = {}
        dashboard_requests: list[dict[str, object]] = []
        last_diagnostic: dict[str, object] = {}

        def request_fault() -> dict[str, object]:
            payload = json.dumps(
                {"task_id": "sensor-sampler", "confirm": True}
            ).encode()
            request = Request(
                f"http://127.0.0.1:{server.server_address[1]}"
                "/api/embedded/fault-injection",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=3) as response:
                accepted = json.loads(response.read())
                if response.status != 202 or accepted.get("success") is not True:
                    self.fail(f"dashboard embedded-fault request rejected: {accepted}")
                return accepted["request"]

        try:
            deadline = time.monotonic() + 25.0
            event_records: list[dict[str, object]] = []
            while time.monotonic() < deadline:
                event_records = runtime.events(500)
                snapshot = runtime.snapshot()
                live_assessments = snapshot["embedded_risk_assessments"]
                for assessment in live_assessments:
                    task_id = assessment["task_id"]
                    if (
                        assessment["risk_level"] == "normal"
                        and assessment["telemetry_freshness"] == "fresh"
                    ):
                        initially_normal_tasks.add(task_id)
                        reserve = assessment["observed_inputs"].get(
                            "stack_high_water_mark_words"
                        )
                        if isinstance(reserve, int):
                            initial_stack_reserves.setdefault(task_id, reserve)
                tasks_are_low_and_fresh = all(
                    assessment["risk_category"] == "LOW"
                    and assessment["telemetry_freshness"] == "fresh"
                    for assessment in live_assessments
                )
                runs = snapshot["embedded_fault_runs"]
                if not dashboard_requests:
                    if (
                        initially_normal_tasks == {"sensor-sampler", "control-loop"}
                        and tasks_are_low_and_fresh
                    ):
                        dashboard_requests.append(request_fault())
                elif len(dashboard_requests) < 2 and runs:
                    previous_run = runs[-1]
                    progress = previous_run.get("restored_progress")
                    latest_sensor = next(
                        (
                            task
                            for task in snapshot["embedded_tasks"]["latest"]
                            if task["task_id"] == "sensor-sampler"
                        ),
                        None,
                    )
                    ready_for_next_run = (
                        previous_run["status"]
                        == "application_state_recovery_verified"
                        and isinstance(progress, dict)
                        and latest_sensor is not None
                        and latest_sensor.get("run_id")
                        == previous_run["run_id"]
                        and latest_sensor["execution_count"] > progress["counter"]
                        and latest_sensor["received_at_utc"]
                        > previous_run["recovery_completed_at_utc"]
                        and tasks_are_low_and_fresh
                    )
                    if ready_for_next_run:
                        dashboard_requests.append(request_fault())

                runs = runtime.snapshot()["embedded_fault_runs"]
                latest_run = runs[-1] if runs else None
                last_diagnostic = {
                    "requested_runs": len(dashboard_requests),
                    "latest_run": latest_run,
                    "latest_tasks": snapshot["embedded_tasks"]["latest"],
                    "assessments": live_assessments,
                }
                latest_progress = (
                    latest_run.get("restored_progress")
                    if latest_run is not None
                    else None
                )
                latest_sensor = next(
                    (
                        task
                        for task in snapshot["embedded_tasks"]["latest"]
                        if task["task_id"] == "sensor-sampler"
                    ),
                    None,
                )
                if (
                    len(dashboard_requests) == 2
                    and latest_run is not None
                    and latest_run["status"]
                    == "application_state_recovery_verified"
                    and isinstance(latest_progress, dict)
                    and latest_sensor is not None
                    and latest_sensor.get("run_id") == latest_run["run_id"]
                    and latest_sensor["execution_count"] > latest_progress["counter"]
                    and latest_sensor["received_at_utc"]
                    > latest_run["recovery_completed_at_utc"]
                    and tasks_are_low_and_fresh
                ):
                    break
                if bridge_error:
                    raise bridge_error[0]
                time.sleep(0.05)
            else:
                self.fail(
                    "two consecutive UART fault/recovery runs did not complete in time: "
                    + json.dumps(last_diagnostic, sort_keys=True)
                )
        finally:
            stop.set()
            bridge_thread.join(timeout=5.0)
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2.0)
            runtime.stop()
            temporary.cleanup()

        self.assertFalse(bridge_thread.is_alive(), "UART bridge did not stop")
        self.assertFalse(bridge_error, bridge_error)
        self.assertEqual(bridge_result, [0])
        self.assertEqual(len(dashboard_requests), 2)
        run_ids = [request["run_id"] for request in dashboard_requests]
        self.assertEqual(len(set(run_ids)), 2)
        self.assertEqual(initially_normal_tasks, {"sensor-sampler", "control-loop"})
        self.assertEqual(set(initial_stack_reserves), {"sensor-sampler", "control-loop"})
        self.assertTrue(all(reserve > 96 for reserve in initial_stack_reserves.values()))
        event_records = runtime.events(500)
        embedded_events = [
            item
            for item in event_records
            if item.get("type") == "embedded_task_event"
        ]
        runs = runtime.snapshot()["embedded_fault_runs"]
        self.assertEqual([run["run_id"] for run in runs[-2:]], run_ids)
        event_sequence = [
                "uart_command_dispatched",
                "fault_injection_requested",
                "fault_injected",
                "embedded_risk_updated",
                "checkpoint_requested",
                "checkpoint_saved",
                "restore_started",
                "task_restart_started",
                "task_restarted",
                "restore_succeeded",
                "task_progress",
                "recovery_completed",
        ]
        for run_id, workflow in zip(run_ids, runs[-2:], strict=True):
            self.assertEqual(workflow["status"], "application_state_recovery_verified")
            self.assertEqual([item["event"] for item in workflow["events"]], event_sequence)
            self.assertTrue(all(item["run_id"] == run_id for item in workflow["events"]))
            self.assertEqual(workflow["checkpoint_id"], run_id)
            saved_state = workflow["checkpoint_saved_state"]
            restored_state = workflow["restored_state"]
            progress = workflow["restored_progress"]
            self.assertEqual(saved_state["checkpoint_size_bytes"], 24)
            self.assertEqual(
                {
                    field: saved_state[field]
                    for field in ("counter", "accumulator", "integrity_hash")
                },
                restored_state,
            )
            self.assertEqual(progress["counter"], saved_state["counter"] + 1)
            self.assertEqual(progress["integrity_hash"], saved_state["integrity_hash"])

        self.assertFalse(
            any(
                item["task_id"] == "control-loop"
                and item["event"]
                in {
                    "checkpoint_requested",
                    "checkpoint_saved",
                    "checkpoint_failed",
                    "task_restart_started",
                    "task_restarted",
                    "restore_succeeded",
                    "restore_failed",
                    "task_progress",
                    "recovery_completed",
                    "recovery_failed",
                }
                for item in embedded_events
            )
        )
        for run_id, workflow in zip(run_ids, runs[-2:], strict=True):
            assessment = workflow["elevated_assessment"]
            self.assertEqual(assessment["risk_category"], "ELEVATED")
            self.assertEqual(assessment["risk_index"], 100)
            transitions = [
                event
                for event in runtime.snapshot()["embedded_risk_events"]
                if event.get("run_id") == run_id
            ]
            elevated_transition = next(
                event for event in transitions
                if event["risk_category"] == "ELEVATED"
            )
            self.assertLessEqual(elevated_transition["stack_reserve_words"], 96)
            self.assertEqual(elevated_transition["threshold_words"], 96)
            self.assertEqual(elevated_transition["risk_index"], 100)
            self.assertTrue(
                any(
                    event["risk_category"] == "LOW"
                    and event["occurred_at_utc"]
                    > elevated_transition["occurred_at_utc"]
                    for event in transitions
                ),
                "risk history must retain the later healthy reassessment",
            )
            self.assertEqual(
                assessment["recommendation"], "checkpoint_recommended"
            )
            self.assertEqual(
                assessment["observed_inputs"]["run_id"], run_id
            )
            self.assertLessEqual(
                assessment["observed_inputs"]["stack_high_water_mark_words"],
                assessment["policy_configuration"][
                    "minimum_stack_high_water_words"
                ],
            )
            checkpoint_event = next(
                event
                for event in workflow["events"]
                if event["event"] == "checkpoint_saved"
            )
            self.assertEqual(
                checkpoint_event["integrity_hash"],
                checkpoint_hash(
                    checkpoint_event["counter"],
                    checkpoint_event["accumulator"],
                    "sensor-sampler",
                ),
            )
        control_samples = [
            item
            for item in event_records
            if item.get("type") == "embedded_task_telemetry"
            and item.get("task_id") == "control-loop"
        ]
        self.assertGreaterEqual(len(control_samples), 2)
        sensor_samples = [
            item
            for item in event_records
            if item.get("type") == "embedded_task_telemetry"
            and item.get("task_id") == "sensor-sampler"
        ]
        for run_id, workflow in zip(run_ids, runs[-2:], strict=True):
            run_samples = [
                item
                for item in sensor_samples
                if item.get("run_id") == run_id
            ]
            self.assertTrue(run_samples, f"no sensor telemetry for run {run_id}")
            self.assertTrue(
                any(item["stack_high_water_mark_words"] <= 96 for item in run_samples),
                f"run {run_id} did not report below-threshold reserve",
            )
            progress_at = next(
                event["occurred_at_utc"]
                for event in workflow["events"]
                if event["event"] == "task_progress"
            )
            post_recovery = [
                item
                for item in run_samples
                if item["received_at_utc"] > progress_at
                and item["execution_count"] > workflow["restored_progress"]["counter"]
            ]
            self.assertTrue(post_recovery, f"no progressing post-recovery telemetry for {run_id}")

        final_snapshot = runtime.snapshot()
        sensor_final = next(
            item
            for item in final_snapshot["embedded_risk_assessments"]
            if item["task_id"] == "sensor-sampler"
        )
        self.assertEqual(sensor_final["risk_category"], "LOW")
        self.assertEqual(sensor_final["recommendation"], "none")
        self.assertEqual(sensor_final["telemetry_freshness"], "fresh")
        self.assertEqual(sensor_final["observed_inputs"]["task_state"], "running")
        live_checkpoint = final_snapshot["embedded_checkpoint_storage"]
        self.assertEqual(live_checkpoint["checkpoint_id"], run_ids[-1])
        self.assertEqual(live_checkpoint["size_bytes"], 24)
        self.assertEqual(live_checkpoint["storage_used_bytes"], 24)
        control_final = next(
            item
            for item in final_snapshot["embedded_risk_assessments"]
            if item["task_id"] == "control-loop"
        )
        self.assertEqual(control_final["risk_category"], "LOW")
        self.assertGreater(
            control_final["observed_inputs"]["stack_high_water_mark_words"], 96
        )
        print(
            "Verified two consecutive live UART runs: "
            f"initial_stack_reserves={initial_stack_reserves}; "
            + "; ".join(
                f"run={workflow['run_id']} "
                f"fault_stack={workflow['elevated_assessment']['observed_inputs']['stack_high_water_mark_words']} "
                f"saved_counter={workflow['checkpoint_saved_state']['counter']} "
                f"restored_counter={workflow['restored_state']['counter']} "
                f"progress_counter={workflow['restored_progress']['counter']}"
                for workflow in runs[-2:]
            )
            + f"; control_loop_samples={len(control_samples)}"
        )


if __name__ == "__main__":
    unittest.main()
