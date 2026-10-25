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


def expected_accumulator(accumulator: int, counter: int) -> int:
    for iteration in range(128):
        accumulator = (
            (accumulator * 33) ^ (counter + iteration + 0x9E3779B9)
        ) & 0xFFFFFFFF
    return accumulator


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
class EmbeddedRecoveryQemuTests(unittest.TestCase):
    def test_sensor_task_checkpoint_restart_restore_and_progress(self) -> None:
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
        deadline = time.monotonic() + 20.0
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
                            process.stdin.write(b"CHECKPOINT sensor-sampler\n")
                            process.stdin.flush()
                            command_sent = True
                        if (
                            record.get("message_type") == "task_event"
                            and record.get("event") == "recovery_completed"
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
        required_sequence = [
            "checkpoint_saved",
            "task_restart_started",
            "task_restarted",
            "restore_succeeded",
            "task_progress",
            "recovery_completed",
        ]
        missing_events = [name for name in required_sequence if name not in event_names]
        self.assertFalse(
            missing_events,
            f"missing events {missing_events}; captured events: {event_names!r}; "
            f"malformed UART lines: {malformed_lines!r}",
        )
        positions = [event_names.index(name) for name in required_sequence]
        self.assertEqual(positions, sorted(positions), event_names)

        checkpoint = next(
            record for record in events if record.get("event") == "checkpoint_saved"
        )
        restarted = next(
            record for record in events if record.get("event") == "task_restarted"
        )
        restored = next(
            record for record in events if record.get("event") == "restore_succeeded"
        )
        progress = next(
            record for record in events if record.get("event") == "task_progress"
        )
        completed = next(
            record for record in events if record.get("event") == "recovery_completed"
        )

        saved_counter = checkpoint["counter"]
        saved_accumulator = checkpoint["accumulator"]
        saved_hash = checkpoint["integrity_hash"]
        self.assertIsInstance(saved_counter, int)
        self.assertIsInstance(saved_accumulator, int)
        self.assertEqual(
            saved_hash, checkpoint_hash(saved_counter, saved_accumulator)
        )
        for record in (restarted, restored):
            self.assertEqual(record["counter"], saved_counter)
            self.assertEqual(record["accumulator"], saved_accumulator)
            self.assertEqual(record["integrity_hash"], saved_hash)
        self.assertEqual(progress["counter"], saved_counter + 1)
        self.assertEqual(
            progress["accumulator"],
            expected_accumulator(saved_accumulator, saved_counter + 1),
        )
        self.assertEqual(completed["counter"], progress["counter"])
        self.assertEqual(completed["accumulator"], progress["accumulator"])

        telemetry = [
            (index, record)
            for index, record in enumerate(records)
            if record.get("message_type") == "task_telemetry"
        ]
        restarted_record_index = records.index(restarted)
        completed_record_index = records.index(completed)
        restored_telemetry = [
            (index, record)
            for index, record in telemetry
            if record.get("task_id") == "sensor-sampler"
            and record.get("execution_count") == saved_counter + 1
        ]
        self.assertTrue(restored_telemetry, "restored task did not emit next count")
        self.assertGreater(restored_telemetry[0][0], records.index(restored))
        self.assertLess(restored_telemetry[0][0], records.index(progress))
        control_after_restart = [
            (index, record)
            for index, record in telemetry
            if record.get("task_id") == "control-loop"
            and restarted_record_index < index < completed_record_index
        ]
        self.assertTrue(
            control_after_restart,
            "control-loop emitted no telemetry while recovery was in progress",
        )
        self.assertGreaterEqual(
            len([record for _, record in telemetry if record.get("task_id") == "control-loop"]),
            2,
        )


if __name__ == "__main__":
    unittest.main()
