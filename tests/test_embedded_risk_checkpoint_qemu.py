from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest

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


def checkpoint_hash(counter: int, accumulator: int) -> int:
    result = 2166136261
    for value in (0x54434F53, 1, 1, counter, accumulator):
        for byte in value.to_bytes(4, "little"):
            result = ((result ^ byte) * 16777619) & 0xFFFFFFFF
    return result


@unittest.skipUnless(
    shutil.which(os.environ.get("QEMU", "qemu-system-riscv32")),
    "qemu-system-riscv32 is unavailable",
)
class EmbeddedRiskCheckpointQemuTests(unittest.TestCase):
    def test_live_policy_recommendation_causes_valid_checkpoint_and_recovery(self) -> None:
        if not FIRMWARE.is_file():
            self.skipTest("Build firmware first with `make -C embedded build`")

        temporary = tempfile.TemporaryDirectory()
        runtime = MonitorRuntime(
            workers=2,
            memory_mib=1,
            sample_interval=0.2,
            checkpoint_interval=0.4,
            embedded_stack_reserve_threshold_words=200,
            checkpoint_dir=Path(temporary.name) / "checkpoints",
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
        try:
            deadline = time.monotonic() + 25.0
            event_records: list[dict[str, object]] = []
            while time.monotonic() < deadline:
                event_records = runtime.events(500)
                live_assessments = runtime.snapshot()["embedded_risk_assessments"]
                if any(
                    item.get("type") == "embedded_task_event"
                    and item.get("event") == "recovery_completed"
                    for item in event_records
                ):
                    break
                if bridge_error:
                    raise bridge_error[0]
                time.sleep(0.05)
            else:
                self.fail("risk-triggered QEMU recovery did not complete in time")
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
        event_records = runtime.events(500)
        embedded_events = [
            item
            for item in event_records
            if item.get("type") == "embedded_task_event"
        ]
        event_names = [item["event"] for item in embedded_events]
        for expected in (
            "checkpoint_requested",
            "checkpoint_saved",
            "task_restart_started",
            "task_restarted",
            "restore_succeeded",
            "task_progress",
            "recovery_completed",
        ):
            self.assertIn(expected, event_names)
        self.assertEqual(event_names.count("checkpoint_requested"), 1)
        checkpoint = next(
            item for item in embedded_events if item["event"] == "checkpoint_saved"
        )
        self.assertEqual(
            checkpoint["integrity_hash"],
            checkpoint_hash(checkpoint["counter"], checkpoint["accumulator"]),
        )
        sensor_assessment = next(
            item for item in live_assessments if item["task_id"] == "sensor-sampler"
        )
        self.assertEqual(sensor_assessment["risk_level"], "elevated")
        self.assertEqual(
            sensor_assessment["recommendation"], "checkpoint_recommended"
        )
        self.assertLessEqual(
            sensor_assessment["observed_inputs"]["stack_high_water_mark_words"],
            sensor_assessment["policy_configuration"][
                "minimum_stack_high_water_words"
            ],
        )
        control_assessment = next(
            item for item in live_assessments if item["task_id"] == "control-loop"
        )
        self.assertEqual(control_assessment["risk_level"], "unknown")
        self.assertEqual(control_assessment["recommendation"], "none")
        control_samples = [
            item
            for item in event_records
            if item.get("type") == "embedded_task_telemetry"
            and item.get("task_id") == "control-loop"
        ]
        self.assertGreaterEqual(len(control_samples), 2)
        print(
            "Verified live embedded policy checkpoint: "
            f"stack_high_water_mark_words="
            f"{sensor_assessment['observed_inputs']['stack_high_water_mark_words']} "
            f"threshold_words="
            f"{sensor_assessment['policy_configuration']['minimum_stack_high_water_words']} "
            f"saved_counter={checkpoint['counter']} "
            f"saved_accumulator={checkpoint['accumulator']} "
            f"integrity_hash={checkpoint['integrity_hash']} "
            f"control_loop_samples={len(control_samples)}"
        )


if __name__ == "__main__":
    unittest.main()
