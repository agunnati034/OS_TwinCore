from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from twincoreos.embedded_telemetry import (
    EmbeddedBridgeError,
    EmbeddedCheckpointGate,
    EmbeddedTelemetryError,
    _post_telemetry,
    parse_uart_record,
    run_bridge,
    validate_task_event,
    validate_task_telemetry,
)


def sample(**changes: object) -> dict[str, object]:
    return {
        "schema_version": 1,
        "message_type": "task_telemetry",
        "target": "freertos-qemu-rv32",
        "task_id": "sensor-sampler",
        "task_state": "running",
        "tick_count": 250,
        "execution_count": 2,
        "stack_high_water_mark_words": 125,
        **changes,
    }


class EmbeddedTelemetryTests(unittest.TestCase):
    def test_valid_task_record_is_parsed_and_validated(self) -> None:
        record = sample()
        parsed = parse_uart_record(
            (json.dumps(record, separators=(",", ":")) + "\n").encode()
        )
        self.assertEqual(parsed, record)

    def test_boot_record_is_ignored_after_schema_validation(self) -> None:
        self.assertIsNone(
            parse_uart_record(
                b'{"schema_version":1,"message_type":"rtos_boot",'
                b'"target":"freertos-qemu-rv32"}\n'
            )
        )

    def test_valid_recovery_event_is_validated_but_not_forwarded_as_telemetry(self) -> None:
        event = {
            "schema_version": 1,
            "message_type": "task_event",
            "target": "freertos-qemu-rv32",
            "task_id": "sensor-sampler",
            "event": "checkpoint_saved",
            "counter": 4,
            "accumulator": 2089706401,
            "integrity_hash": 3568767710,
            "checkpoint_size_bytes": 24,
        }
        line = (json.dumps(event, separators=(",", ":")) + "\n").encode()
        self.assertEqual(validate_task_event(event), event)
        self.assertIsNone(parse_uart_record(line))
        self.assertEqual(parse_uart_record(line, include_events=True), event)

    def test_checkpoint_policy_gate_deduplicates_and_rearms_after_clear(self) -> None:
        gate = EmbeddedCheckpointGate()

        def response(
            risk_level: str,
            recommendation: str,
            stack_words: int,
            rules: list[str],
            task_id: str = "sensor-sampler",
        ) -> dict[str, object]:
            return {
                "assessment_type": "heuristic_operational_risk",
                "assessments": [{
                    "assessment_type": "heuristic_operational_risk",
                    "policy_id": "embedded-stack-reserve-v1",
                    "target": "freertos-qemu-rv32",
                    "task_id": task_id,
                    "risk_level": risk_level,
                    "recommendation": recommendation,
                    "matched_rules": rules,
                    "observed_inputs": {
                        "task_state": "running",
                        "stack_high_water_mark_words": stack_words,
                        "telemetry_age_seconds": 1.0,
                    },
                    "policy_configuration": {
                        "minimum_stack_high_water_words": 32,
                        "stale_after_seconds": 3.0,
                    },
                }],
            }

        elevated = response(
            "elevated",
            "checkpoint_recommended",
            32,
            ["stack_high_water_at_or_below_configured_minimum"],
        )
        normal = response("normal", "none", 33, [])
        self.assertTrue(gate.should_request(elevated))
        self.assertFalse(gate.should_request(elevated))
        gate.observe_event({"event": "checkpoint_saved"})
        self.assertFalse(gate.should_request(elevated))
        self.assertFalse(gate.should_request(normal))
        self.assertTrue(gate.should_request(elevated))

        control_elevated = response(
            "elevated",
            "checkpoint_recommended",
            16,
            ["stack_high_water_at_or_below_configured_minimum"],
            task_id="control-loop",
        )
        self.assertFalse(gate.should_request(control_elevated, "control-loop"))
        self.assertFalse(gate.should_request(elevated))
        self.assertFalse(gate.should_request(elevated, "unsupported-task"))
        gate.observe_event({"task_id": [], "event": "checkpoint_saved"})

    def test_checkpoint_policy_gate_fails_closed_and_latches_failure(self) -> None:
        gate = EmbeddedCheckpointGate()
        invalid_responses = [{"assessment_type": "wrong", "assessments": []}]
        for response in invalid_responses:
            self.assertFalse(gate.should_request(response))

        gate_for_failure = EmbeddedCheckpointGate()
        elevated = {
            "assessment_type": "heuristic_operational_risk",
            "assessments": [{
                "assessment_type": "heuristic_operational_risk",
                "policy_id": "embedded-stack-reserve-v1",
                "target": "freertos-qemu-rv32",
                "task_id": "sensor-sampler",
                "risk_level": "elevated",
                "recommendation": "checkpoint_recommended",
                "matched_rules": ["stack_high_water_at_or_below_configured_minimum"],
                "observed_inputs": {
                    "task_state": "running",
                    "stack_high_water_mark_words": 32,
                    "telemetry_age_seconds": 1.0,
                },
                "policy_configuration": {
                    "minimum_stack_high_water_words": 32,
                    "stale_after_seconds": 3.0,
                },
            }],
        }
        normal = {
            "assessment_type": "heuristic_operational_risk",
            "assessments": [{
                "assessment_type": "heuristic_operational_risk",
                "policy_id": "embedded-stack-reserve-v1",
                "target": "freertos-qemu-rv32",
                "task_id": "sensor-sampler",
                "risk_level": "normal",
                "recommendation": "none",
                "matched_rules": [],
                "observed_inputs": {
                    "task_state": "running",
                    "stack_high_water_mark_words": 33,
                    "telemetry_age_seconds": 1.0,
                },
                "policy_configuration": {
                    "minimum_stack_high_water_words": 32,
                    "stale_after_seconds": 3.0,
                },
            }],
        }
        self.assertTrue(gate_for_failure.should_request(elevated))
        gate_for_failure.observe_event({"event": "checkpoint_failed"})
        self.assertFalse(gate_for_failure.should_request(elevated))
        self.assertFalse(gate_for_failure.should_request(normal))
        self.assertTrue(gate_for_failure.should_request(elevated))

    def test_invalid_recovery_events_are_rejected(self) -> None:
        base = {
            "schema_version": 1,
            "message_type": "task_event",
            "target": "freertos-qemu-rv32",
            "task_id": "sensor-sampler",
            "event": "checkpoint_saved",
            "counter": 4,
            "accumulator": 2089706401,
            "integrity_hash": 3568767710,
            "checkpoint_size_bytes": 24,
        }
        invalid_events = [
            {key: value for key, value in base.items() if key != "integrity_hash"},
            {**base, "integrity_hash": base["integrity_hash"] ^ 1},
            {**base, "event": "unrecognized"},
            {**base, "task_id": "worker-1"},
            {**base, "counter": True},
            {**base, "extra": "not in schema"},
            {
                key: value
                for key, value in base.items()
                if key not in {"counter", "accumulator", "integrity_hash"}
            },
        ]
        for event in invalid_events:
            with self.subTest(event=event), self.assertRaises(EmbeddedTelemetryError):
                validate_task_event(event)

    def test_checkpoint_saved_requires_valid_firmware_record_size(self) -> None:
        base = {
            "schema_version": 1,
            "message_type": "task_event",
            "target": "freertos-qemu-rv32",
            "task_id": "sensor-sampler",
            "event": "checkpoint_saved",
            "counter": 4,
            "accumulator": 2089706401,
            "integrity_hash": 3568767710,
            "checkpoint_size_bytes": 24,
        }
        for invalid_size in (None, True, 0, -1, 1025, 1.5):
            with self.subTest(size=invalid_size), self.assertRaisesRegex(
                EmbeddedTelemetryError, "checkpoint_saved requires"
            ):
                validate_task_event({**base, "checkpoint_size_bytes": invalid_size})
        missing_size = {
            key: value for key, value in base.items() if key != "checkpoint_size_bytes"
        }
        with self.assertRaisesRegex(
            EmbeddedTelemetryError, "checkpoint_saved requires"
        ):
            validate_task_event(missing_size)

    def test_non_checkpoint_event_rejects_checkpoint_size(self) -> None:
        event = {
            "schema_version": 1,
            "message_type": "task_event",
            "target": "freertos-qemu-rv32",
            "task_id": "sensor-sampler",
            "event": "task_progress",
            "checkpoint_size_bytes": 24,
        }
        with self.assertRaisesRegex(
            EmbeddedTelemetryError, "task event fields do not match schema"
        ):
            validate_task_event(event)

    def test_control_loop_checkpoint_event_is_not_supported(self) -> None:
        from twincoreos.embedded_telemetry import _checkpoint_integrity_hash

        with self.assertRaisesRegex(
            EmbeddedTelemetryError, "checkpoint task identity is unsupported"
        ):
            _checkpoint_integrity_hash(4, 2089706401, "control-loop")

        event = {
            "schema_version": 1,
            "message_type": "task_event",
            "target": "freertos-qemu-rv32",
            "task_id": "control-loop",
            "event": "checkpoint_saved",
            "counter": 4,
            "accumulator": 2089706401,
            "integrity_hash": 0,
            "checkpoint_size_bytes": 24,
        }
        with self.assertRaisesRegex(
            EmbeddedTelemetryError, "checkpoint task identity is unsupported"
        ):
            validate_task_event(event)

    def test_fault_injection_events_are_valid_task_events(self) -> None:
        for name in ("fault_injection_requested", "fault_injected"):
            event = {
                "schema_version": 1,
                "message_type": "task_event",
                "target": "freertos-qemu-rv32",
                "task_id": "sensor-sampler",
                "event": name,
            }
            self.assertEqual(validate_task_event(event), event)

    def test_failure_events_are_not_misreported_as_checkpoint_success(self) -> None:
        failure = {
            "schema_version": 1,
            "message_type": "task_event",
            "target": "freertos-qemu-rv32",
            "task_id": "sensor-sampler",
            "event": "checkpoint_failed",
        }
        self.assertEqual(validate_task_event(failure), failure)
        failed_restore = {
            **failure,
            "event": "restore_failed",
            "counter": 4,
            "accumulator": 2089706401,
            "integrity_hash": 0,
        }
        self.assertEqual(validate_task_event(failed_restore), failed_restore)
        failed_recovery = {**failure, "event": "recovery_failed"}
        self.assertEqual(validate_task_event(failed_recovery), failed_recovery)

        invalid_restore = {
            **failure,
            "event": "restore_succeeded",
            "counter": 4,
            "accumulator": 2089706401,
            "integrity_hash": 1931413350,
        }
        with self.assertRaisesRegex(
            EmbeddedTelemetryError, "integrity hash does not match"
        ):
            validate_task_event(invalid_restore)

    def test_progress_event_carries_original_checkpoint_hash(self) -> None:
        progress = {
            "schema_version": 1,
            "message_type": "task_event",
            "target": "freertos-qemu-rv32",
            "task_id": "sensor-sampler",
            "event": "task_progress",
            "counter": 5,
            "accumulator": 640760704,
            "integrity_hash": 3568767710,
        }
        self.assertEqual(validate_task_event(progress), progress)

    def test_malformed_and_incomplete_uart_records_are_rejected(self) -> None:
        for line in (b"{bad json}\n", b'{"message_type":"task_telemetry"}'):
            with self.subTest(line=line), self.assertRaises(EmbeddedTelemetryError):
                parse_uart_record(line)

    def test_missing_invalid_and_extra_fields_are_rejected(self) -> None:
        invalid_records = [
            {key: value for key, value in sample().items() if key != "task_state"},
            sample(schema_version=2),
            sample(task_id=""),
            sample(task_state=[]),
            sample(execution_count=True),
            sample(tick_count=-1),
            sample(extra="not in schema"),
        ]
        for record in invalid_records:
            with self.subTest(record=record), self.assertRaises(
                EmbeddedTelemetryError
            ):
                validate_task_telemetry(record)

    def test_backend_http_error_is_explicit(self) -> None:
        error = HTTPError(
            "http://127.0.0.1:8765/api/embedded/telemetry",
            400,
            "invalid",
            {},
            io.BytesIO(b'{"success":false,"error":"bad sample"}'),
        )
        with patch(
            "twincoreos.embedded_telemetry.urlopen", side_effect=error
        ) as opener, self.assertRaisesRegex(EmbeddedBridgeError, "HTTP 400"):
            _post_telemetry(
                "http://127.0.0.1:8765/api/embedded/telemetry",
                "0d179e32-3fbc-4ee1-830b-b69256131d89",
                sample(),
                opener,
            )

    def test_bridge_shutdown_terminates_owned_qemu_process(self) -> None:
        class FakeProcess:
            def __init__(self, stdout: object, writer: int) -> None:
                self.stdout = stdout
                self.stdin = io.BytesIO()
                self.writer = writer
                self.returncode: int | None = None
                self.terminated = False

            def poll(self) -> int | None:
                return self.returncode

            def terminate(self) -> None:
                self.terminated = True
                self.returncode = -15
                if self.writer >= 0:
                    os.close(self.writer)

            def wait(self, timeout: float) -> int:
                return self.returncode or 0

        with tempfile.TemporaryDirectory() as directory:
            firmware = Path(directory) / "firmware.elf"
            firmware.touch()
            reader, writer = os.pipe()
            process = FakeProcess(os.fdopen(reader, "rb", buffering=0), writer)
            with patch(
                "twincoreos.embedded_telemetry.subprocess.Popen",
                return_value=process,
            ) as popen:
                result = run_bridge(
                    firmware,
                    "http://127.0.0.1:8765/api/embedded/telemetry",
                    stop_requested=lambda: True,
                )
            self.assertEqual(result, 0)
            self.assertTrue(process.terminated)
            self.assertIn("-serial", popen.call_args.args[0])
            self.assertIn("stdio", popen.call_args.args[0])

    def test_bridge_reports_uart_disconnect_and_stops_qemu(self) -> None:
        class FakeProcess:
            def __init__(self, stdout: object, writer: int) -> None:
                self.stdout = stdout
                self.stdin = io.BytesIO()
                self.writer = writer
                self.returncode: int | None = None
                self.terminated = False

            def poll(self) -> int | None:
                return self.returncode

            def terminate(self) -> None:
                self.terminated = True
                self.returncode = -15
                if self.writer >= 0:
                    os.close(self.writer)

            def wait(self, timeout: float) -> int:
                return self.returncode or 0

        with tempfile.TemporaryDirectory() as directory:
            firmware = Path(directory) / "firmware.elf"
            firmware.touch()
            reader, writer = os.pipe()
            os.close(writer)
            process = FakeProcess(os.fdopen(reader, "rb", buffering=0), -1)
            with patch(
                "twincoreos.embedded_telemetry.subprocess.Popen",
                return_value=process,
            ), self.assertRaisesRegex(EmbeddedBridgeError, "disconnected"):
                run_bridge(
                    firmware,
                    "http://127.0.0.1:8765/api/embedded/telemetry",
                )
            self.assertTrue(process.terminated)


if __name__ == "__main__":
    unittest.main()
