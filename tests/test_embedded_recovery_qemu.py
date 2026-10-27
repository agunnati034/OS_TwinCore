from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import time
import unittest


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


def expected_accumulator(
    accumulator: int, counter: int, work_iterations: int = 128
) -> int:
    for iteration in range(work_iterations):
        accumulator = (
            (accumulator * 33) ^ (counter + iteration + 0x9E3779B9)
        ) & 0xFFFFFFFF
    return accumulator


def checkpoint_hash(
    counter: int, accumulator: int, task_id: str = "sensor-sampler"
) -> int:
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
class EmbeddedRecoveryQemuTests(unittest.TestCase):
    def test_sensor_checkpoint_recovers_and_control_loop_is_not_checkpointed(self) -> None:
        if not FIRMWARE.is_file():
            self.skipTest("Build firmware first with `make -C embedded build`")

        command = [
            os.environ.get("QEMU", "qemu-system-riscv32"),
            "-machine",
            "virt",
            "-net",
            "none",
            "-serial",
            "stdio",
            "-monitor",
            "none",
            "-display",
            "none",
            "-bios",
            "none",
            "-smp",
            "1",
            "-kernel",
            str(FIRMWARE),
        ]
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self.assertIsNotNone(process.stdout)
        selector = selectors.DefaultSelector()
        records: list[dict[str, object]] = []
        malformed_lines: list[str] = []
        buffer = bytearray()
        command_sent = False
        invalid_command_sent = False
        deadline = time.monotonic() + 30.0
        try:
            assert process.stdout is not None
            descriptor = process.stdout.fileno()
            os.set_blocking(descriptor, False)
            selector.register(descriptor, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                ready = selector.select(timeout=0.25)
                if not ready:
                    if process.poll() is not None:
                        break
                    continue
                chunk = os.read(descriptor, 4096)
                if not chunk:
                    break
                for byte in chunk:
                    if byte != 10:
                        buffer.append(byte)
                        continue
                    try:
                        record = json.loads(buffer.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        malformed_lines.append(buffer.decode("utf-8", errors="replace"))
                        buffer.clear()
                        continue
                    buffer.clear()
                    if isinstance(record, dict):
                        records.append(record)
                        if (
                            not command_sent
                            and record.get("message_type") == "task_telemetry"
                            and record.get("task_id") == "sensor-sampler"
                        ):
                            self.assertIsNotNone(process.stdin)
                            process.stdin.write(b"CHECKPOINT control-loop\n")
                            process.stdin.flush()
                            invalid_command_sent = True
                            process.stdin.write(b"CHECKPOINT sensor-sampler\n")
                            process.stdin.flush()
                            command_sent = True
                        if (
                            record.get("message_type") == "task_event"
                            and record.get("event") == "recovery_completed"
                            and record.get("task_id") == "sensor-sampler"
                        ):
                            break
                else:
                    continue
                break
        finally:
            selector.close()
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3.0)
            if process.stdout is not None:
                process.stdout.close()
            if process.stdin is not None:
                process.stdin.close()

        events = [
            record
            for record in records
            if record.get("message_type") == "task_event"
        ]
        event_names = [record.get("event") for record in events]
        self.assertNotIn("recovery_failed", event_names, event_names)
        self.assertTrue(invalid_command_sent)
        self.assertTrue(command_sent)
        self.assertEqual(
            [
                record["task_id"]
                for record in events
                if record.get("event") == "checkpoint_requested"
            ],
            ["sensor-sampler"],
        )
        self.assertFalse(
            any(
                record.get("task_id") == "control-loop"
                and record.get("event")
                in {
                    "checkpoint_requested",
                    "checkpoint_saved",
                    "checkpoint_failed",
                    "task_restart_started",
                    "task_restarted",
                    "restore_started",
                    "restore_succeeded",
                    "restore_failed",
                    "task_progress",
                    "recovery_completed",
                    "recovery_failed",
                }
                for record in events
            ),
            "control-loop must not emit checkpoint or recovery events",
        )
        required_sequence = [
            "checkpoint_saved",
            "restore_started",
            "task_restart_started",
            "task_restarted",
            "restore_succeeded",
            "task_progress",
            "recovery_completed",
        ]
        missing_events = [
            f"{task_id}:{name}"
            for task_id in ("sensor-sampler",)
            for name in required_sequence
            if not any(
                record.get("task_id") == task_id
                and record.get("event") == name
                for record in events
            )
        ]
        self.assertFalse(
            missing_events,
            f"missing events {missing_events}; captured events: {event_names!r}; "
            f"malformed UART lines: {malformed_lines!r}",
        )
        self.assertEqual(
            sum(name == "task_restart_started" for name in event_names),
            1,
            event_names,
        )
        self.assertEqual(
            sum(name == "task_restarted" for name in event_names),
            1,
            event_names,
        )
        saved_states: dict[str, tuple[int, int]] = {}
        for task_id, work_iterations in (("sensor-sampler", 128),):
            task_events = [
                record for record in events if record.get("task_id") == task_id
            ]
            positions = [
                next(
                    index
                    for index, record in enumerate(task_events)
                    if record.get("event") == name
                )
                for name in required_sequence
            ]
            self.assertEqual(positions, sorted(positions), task_events)
            checkpoint = next(
                record
                for record in task_events
                if record.get("event") == "checkpoint_saved"
            )
            restarted = next(
                record
                for record in task_events
                if record.get("event") == "task_restarted"
            )
            restored = next(
                record
                for record in task_events
                if record.get("event") == "restore_succeeded"
            )
            progress = next(
                record
                for record in task_events
                if record.get("event") == "task_progress"
            )
            completed = next(
                record
                for record in task_events
                if record.get("event") == "recovery_completed"
            )

            saved_counter = checkpoint["counter"]
            saved_accumulator = checkpoint["accumulator"]
            saved_hash = checkpoint["integrity_hash"]
            self.assertIsInstance(saved_counter, int)
            self.assertIsInstance(saved_accumulator, int)
            self.assertEqual(
                saved_hash,
                checkpoint_hash(saved_counter, saved_accumulator, task_id),
            )
            for record in (restarted, restored):
                self.assertEqual(record["counter"], saved_counter)
                self.assertEqual(record["accumulator"], saved_accumulator)
                self.assertEqual(record["integrity_hash"], saved_hash)
            self.assertEqual(progress["counter"], saved_counter + 1)
            self.assertEqual(
                progress["accumulator"],
                expected_accumulator(
                    saved_accumulator, saved_counter + 1, work_iterations
                ),
            )
            self.assertEqual(completed["counter"], progress["counter"])
            self.assertEqual(completed["accumulator"], progress["accumulator"])
            saved_states[task_id] = (saved_counter, saved_accumulator)

        telemetry = [
            (index, record)
            for index, record in enumerate(records)
            if record.get("message_type") == "task_telemetry"
        ]
        sensor_restored_index = records.index(
            next(
                record
                for record in events
                if record.get("task_id") == "sensor-sampler"
                and record.get("event") == "restore_succeeded"
            )
        )
        restored_telemetry = [
            (index, record)
            for index, record in telemetry
            if record.get("task_id") == "sensor-sampler"
            and index > sensor_restored_index
            and record.get("execution_count")
            == saved_states["sensor-sampler"][0] + 1
        ]
        self.assertTrue(restored_telemetry, "restored task did not emit next count")
        self.assertGreater(restored_telemetry[0][0], sensor_restored_index)
        self.assertLess(restored_telemetry[0][0], records.index(progress))
        control_during_recovery = [
            (index, record)
            for index, record in telemetry
            if record.get("task_id") == "control-loop"
            and sensor_restored_index < index < records.index(completed)
        ]
        self.assertTrue(
            control_during_recovery,
            "control-loop telemetry did not continue while sensor-sampler recovered",
        )
        self.assertGreaterEqual(
            len([record for _, record in telemetry if record.get("task_id") == "control-loop"]),
            2,
        )


if __name__ == "__main__":
    unittest.main()
