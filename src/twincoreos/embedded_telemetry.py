"""Validation and transport for the FreeRTOS-on-QEMU telemetry stream."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import selectors
import subprocess
import sys
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
import uuid


TARGET = "freertos-qemu-rv32"
SCHEMA_VERSION = 1
CHECKPOINT_MAGIC = 0x54434F53
CHECKPOINT_SCHEMA_VERSION = 1
MAX_UART_LINE_BYTES = 4096
TASK_STATES = {"running", "ready", "blocked", "suspended", "deleted"}
TASK_IDS = {"sensor-sampler", "control-loop"}
TASK_CHECKPOINT_IDS = {"sensor-sampler": 1}
TASK_EVENTS = {
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
    "fault_injection_requested",
    "fault_injected",
}
TASK_EVENT_STATE_FIELDS = {"counter", "accumulator", "integrity_hash"}
TASK_EVENT_CHECKPOINT_FIELDS = {"checkpoint_size_bytes"}
RUN_ID_FIELD = "run_id"
CHECKPOINT_SUCCESS_EVENTS = {
    "checkpoint_saved",
    "task_restart_started",
    "task_restarted",
    "restore_started",
    "restore_succeeded",
}


class EmbeddedTelemetryError(ValueError):
    """An embedded task telemetry record is invalid or incompatible."""


class EmbeddedBridgeError(RuntimeError):
    """The QEMU UART bridge could not deliver or maintain its stream."""


def _checkpoint_integrity_hash(
    counter: int, accumulator: int, task_id: str = "sensor-sampler"
) -> int:
    checkpoint_task_id = TASK_CHECKPOINT_IDS.get(task_id)
    if checkpoint_task_id is None:
        raise EmbeddedTelemetryError("checkpoint task identity is unsupported")
    result = 2166136261
    for value in (
        CHECKPOINT_MAGIC,
        CHECKPOINT_SCHEMA_VERSION,
        checkpoint_task_id,
        counter,
        accumulator,
    ):
        for byte in value.to_bytes(4, "little"):
            result = ((result ^ byte) * 16777619) & 0xFFFFFFFF
    return result


def _validate_run_id(value: object) -> str:
    if not isinstance(value, str):
        raise EmbeddedTelemetryError("run_id must be a canonical UUID string")
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError("UUID must use canonical form")
    except ValueError as error:
        raise EmbeddedTelemetryError(
            "run_id must be a canonical UUID string"
        ) from error
    return value


def validate_task_telemetry(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EmbeddedTelemetryError("telemetry must be a JSON object")
    expected = {
        "schema_version",
        "message_type",
        "target",
        "task_id",
        "task_state",
        "tick_count",
        "execution_count",
        "stack_high_water_mark_words",
    }
    fields = set(value)
    if not expected <= fields or fields - expected - {RUN_ID_FIELD}:
        missing = sorted(expected - fields)
        extra = sorted(fields - expected - {RUN_ID_FIELD})
        raise EmbeddedTelemetryError(
            f"telemetry fields do not match schema v1 (missing={missing}, extra={extra})"
        )
    if RUN_ID_FIELD in value:
        _validate_run_id(value[RUN_ID_FIELD])
    if (
        not isinstance(value["schema_version"], int)
        or isinstance(value["schema_version"], bool)
        or value["schema_version"] != SCHEMA_VERSION
    ):
        raise EmbeddedTelemetryError("unsupported telemetry schema_version")
    if value["message_type"] != "task_telemetry":
        raise EmbeddedTelemetryError("message_type must be task_telemetry")
    if value["target"] != TARGET:
        raise EmbeddedTelemetryError(f"target must be {TARGET}")
    task_id = value["task_id"]
    if (
        not isinstance(task_id, str)
        or not task_id
        or len(task_id) > 64
        or not task_id.replace("-", "").replace("_", "").isalnum()
    ):
        raise EmbeddedTelemetryError("task_id must be a non-empty task identifier")
    if not isinstance(value["task_state"], str) or value["task_state"] not in TASK_STATES:
        raise EmbeddedTelemetryError("task_state is not a supported FreeRTOS state")
    for field in ("tick_count", "execution_count", "stack_high_water_mark_words"):
        item = value[field]
        minimum = 1 if field == "execution_count" else 0
        if (
            not isinstance(item, int)
            or isinstance(item, bool)
            or not minimum <= item <= 0xFFFFFFFF
        ):
            raise EmbeddedTelemetryError(
                f"{field} must be an integer in [{minimum}, 4294967295]"
            )
    return dict(value)


def validate_task_event(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EmbeddedTelemetryError("task event must be a JSON object")
    required = {
        "schema_version",
        "message_type",
        "target",
        "task_id",
        "event",
    }
    fields = frozenset(value)
    allowed = (
        required
        | TASK_EVENT_STATE_FIELDS
        | TASK_EVENT_CHECKPOINT_FIELDS
        | {RUN_ID_FIELD}
    )
    if not required <= fields or fields - allowed or (
        (
            fields
            - {RUN_ID_FIELD}
            - TASK_EVENT_CHECKPOINT_FIELDS
        )
        not in {
            frozenset(required),
            frozenset(required | TASK_EVENT_STATE_FIELDS),
        }
        or (
            TASK_EVENT_CHECKPOINT_FIELDS & fields
            and value["event"] != "checkpoint_saved"
        )
    ):
        missing = sorted(required - set(fields))
        extra = sorted(set(fields) - allowed)
        raise EmbeddedTelemetryError(
            f"task event fields do not match schema v1 (missing={missing}, extra={extra})"
        )
    if RUN_ID_FIELD in value:
        _validate_run_id(value[RUN_ID_FIELD])
    if (
        not isinstance(value["schema_version"], int)
        or isinstance(value["schema_version"], bool)
        or value["schema_version"] != SCHEMA_VERSION
    ):
        raise EmbeddedTelemetryError("unsupported task event schema_version")
    if value["message_type"] != "task_event" or value["target"] != TARGET:
        raise EmbeddedTelemetryError("task event message_type or target is invalid")
    if (
        not isinstance(value["task_id"], str)
        or value["task_id"] not in TASK_IDS
    ):
        raise EmbeddedTelemetryError("task event task_id is unsupported")
    if not isinstance(value["event"], str) or value["event"] not in TASK_EVENTS:
        raise EmbeddedTelemetryError("task event name is not supported")
    if value["event"] == "checkpoint_saved":
        size = value.get("checkpoint_size_bytes")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or not 1 <= size <= 1024
        ):
            raise EmbeddedTelemetryError(
                "checkpoint_saved requires a valid checkpoint_size_bytes"
            )
    if "counter" in value:
        for field in TASK_EVENT_STATE_FIELDS:
            item = value[field]
            if (
                not isinstance(item, int)
                or isinstance(item, bool)
                or not 0 <= item <= 0xFFFFFFFF
            ):
                raise EmbeddedTelemetryError(
                    f"task event {field} must be an unsigned 32-bit integer"
                )
    if value["event"] in CHECKPOINT_SUCCESS_EVENTS and "counter" not in value:
        raise EmbeddedTelemetryError(f"{value['event']} requires checkpoint state")
    if (
        value["event"] in CHECKPOINT_SUCCESS_EVENTS
        and value["integrity_hash"]
        != _checkpoint_integrity_hash(
            value["counter"], value["accumulator"], value["task_id"]
        )
    ):
        raise EmbeddedTelemetryError(
            f"{value['event']} checkpoint integrity hash does not match its state"
        )
    return dict(value)


def parse_uart_record(
    line: bytes, *, include_events: bool = False
) -> dict[str, Any] | None:
    """Parse one UART line; ignore validated boot and recovery-event records."""
    if not line.endswith(b"\n"):
        raise EmbeddedTelemetryError("incomplete UART record (missing newline)")
    if len(line) > MAX_UART_LINE_BYTES:
        raise EmbeddedTelemetryError("UART record exceeds the maximum line size")
    try:
        record = json.loads(line.decode("utf-8").strip())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EmbeddedTelemetryError(f"malformed UART JSON record: {error}") from error
    if not isinstance(record, dict):
        raise EmbeddedTelemetryError("UART record must be a JSON object")
    message_type = record.get("message_type")
    if message_type == "rtos_boot":
        if record.get("schema_version") != SCHEMA_VERSION or record.get("target") != TARGET:
            raise EmbeddedTelemetryError("incompatible RTOS boot record")
        return None
    if message_type == "startup_error":
        raise EmbeddedTelemetryError(f"FreeRTOS firmware reported startup error: {record}")
    if message_type == "task_event":
        validate_task_event(record)
        return record if include_events else None
    return validate_task_telemetry(record)


def _post_telemetry(
    endpoint: str,
    bridge_session_id: str,
    record: dict[str, Any],
    opener: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    payload = json.dumps(
        {"bridge_session_id": bridge_session_id, "telemetry": record},
        separators=(",", ":"),
    ).encode("utf-8")
    request = Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with opener(request, timeout=3) as response:
            result = json.loads(response.read())
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        error.close()
        raise EmbeddedBridgeError(
            f"backend rejected telemetry with HTTP {error.code}: {detail}"
        ) from error
    except (
        URLError,
        TimeoutError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as error:
        raise EmbeddedBridgeError(f"could not deliver telemetry to backend: {error}") from error
    if not isinstance(result, dict) or result.get("success") is not True:
        raise EmbeddedBridgeError("backend returned an unsuccessful telemetry response")
    return result


def _post_task_event(
    endpoint: str,
    bridge_session_id: str,
    record: dict[str, Any],
    opener: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    payload = json.dumps(
        {"bridge_session_id": bridge_session_id, "event": record},
        separators=(",", ":"),
    ).encode("utf-8")
    request = Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with opener(request, timeout=3) as response:
            result = json.loads(response.read())
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        error.close()
        raise EmbeddedBridgeError(
            f"backend rejected embedded event with HTTP {error.code}: {detail}"
        ) from error
    except (
        URLError,
        TimeoutError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as error:
        raise EmbeddedBridgeError(f"could not deliver embedded event: {error}") from error
    if not isinstance(result, dict) or result.get("success") is not True:
        raise EmbeddedBridgeError("backend returned an unsuccessful event response")
    return result


def _get_embedded_risk(
    endpoint: str, opener: Callable[..., Any] = urlopen
) -> dict[str, Any]:
    request = Request(endpoint, headers={"Accept": "application/json"})
    try:
        with opener(request, timeout=3) as response:
            result = json.loads(response.read())
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        error.close()
        raise EmbeddedBridgeError(
            f"could not read embedded risk assessment: HTTP {error.code}: {detail}"
        ) from error
    except (
        URLError,
        TimeoutError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as error:
        raise EmbeddedBridgeError(f"could not read embedded risk assessment: {error}") from error
    if (
        not isinstance(result, dict)
        or result.get("assessment_type") != "heuristic_operational_risk"
        or not isinstance(result.get("assessments"), list)
    ):
        raise EmbeddedBridgeError("backend returned an invalid embedded risk response")
    return result


def _get_embedded_uart_command(
    endpoint: str,
    bridge_session_id: str,
    opener: Callable[..., Any] = urlopen,
) -> dict[str, str] | None:
    request = Request(
        f"{endpoint}?{urlencode({'bridge_session_id': bridge_session_id})}",
        headers={"Accept": "application/json"},
    )
    try:
        with opener(request, timeout=3) as response:
            result = json.loads(response.read())
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        error.close()
        raise EmbeddedBridgeError(
            f"could not poll embedded UART commands: HTTP {error.code}: {detail}"
        ) from error
    except (
        URLError,
        TimeoutError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as error:
        raise EmbeddedBridgeError(
            f"could not poll embedded UART commands: {error}"
        ) from error
    if not isinstance(result, dict):
        raise EmbeddedBridgeError("backend returned an invalid UART command response")
    command = result.get("command")
    if command is None:
        return None
    if (
        not isinstance(command, dict)
        or set(command) != {"request_id", "run_id", "command"}
        or not isinstance(command["request_id"], str)
        or command["request_id"] != command["run_id"]
        or command["command"]
        != f"INJECT_STACK_PRESSURE sensor-sampler {command['run_id']}"
    ):
        raise EmbeddedBridgeError("backend returned an unsupported UART command")
    _validate_run_id(command["run_id"])
    return command


def _assessment_recommends_checkpoint(
    response: dict[str, Any],
    task_id: str = "sensor-sampler",
    run_id: str | None = None,
) -> bool:
    if response.get("assessment_type") != "heuristic_operational_risk":
        return False
    assessments = response.get("assessments")
    if not isinstance(assessments, list):
        return False
    for assessment in assessments:
        if not isinstance(assessment, dict) or assessment.get("task_id") != task_id:
            continue
        if (
            assessment.get("assessment_type") != "heuristic_operational_risk"
            or assessment.get("policy_id") != "embedded-stack-reserve-v1"
            or assessment.get("target") != TARGET
        ):
            return False
        observed = assessment.get("observed_inputs")
        configuration = assessment.get("policy_configuration")
        rules = assessment.get("matched_rules")
        if not isinstance(observed, dict) or not isinstance(configuration, dict):
            return False
        stack_words = observed.get("stack_high_water_mark_words")
        age = observed.get("telemetry_age_seconds")
        observed_run_id = observed.get(RUN_ID_FIELD)
        if run_id is not None and observed_run_id != run_id:
            return False
        minimum = configuration.get("minimum_stack_high_water_words")
        stale_after = configuration.get("stale_after_seconds")
        return (
            assessment.get("risk_level") == "elevated"
            and assessment.get("recommendation") == "checkpoint_recommended"
            and rules == ["stack_high_water_at_or_below_configured_minimum"]
            and isinstance(stack_words, int)
            and not isinstance(stack_words, bool)
            and isinstance(minimum, int)
            and not isinstance(minimum, bool)
            and stack_words <= minimum
            and isinstance(age, (int, float))
            and not isinstance(age, bool)
            and math.isfinite(age)
            and age >= 0
            and observed.get("task_state") == "running"
            and isinstance(stale_after, (int, float))
            and not isinstance(stale_after, bool)
            and math.isfinite(stale_after)
            and stale_after > 0
            and age <= stale_after
        )
    return False


def _assessment_is_normal_and_fresh(
    response: dict[str, Any], task_id: str = "sensor-sampler"
) -> bool:
    if response.get("assessment_type") != "heuristic_operational_risk":
        return False
    assessments = response.get("assessments")
    if not isinstance(assessments, list):
        return False
    for assessment in assessments:
        if not isinstance(assessment, dict) or assessment.get("task_id") != task_id:
            continue
        observed = assessment.get("observed_inputs")
        configuration = assessment.get("policy_configuration")
        if (
            assessment.get("assessment_type") != "heuristic_operational_risk"
            or assessment.get("policy_id") != "embedded-stack-reserve-v1"
            or assessment.get("target") != TARGET
            or assessment.get("risk_level") != "normal"
            or assessment.get("recommendation") != "none"
            or assessment.get("matched_rules") != []
            or not isinstance(observed, dict)
            or not isinstance(configuration, dict)
        ):
            return False
        stack_words = observed.get("stack_high_water_mark_words")
        age = observed.get("telemetry_age_seconds")
        minimum = configuration.get("minimum_stack_high_water_words")
        stale_after = configuration.get("stale_after_seconds")
        if (
            not isinstance(stale_after, (int, float))
            or isinstance(stale_after, bool)
            or not math.isfinite(stale_after)
            or stale_after <= 0
        ):
            return False
        return (
            observed.get("task_state") == "running"
            and isinstance(stack_words, int)
            and not isinstance(stack_words, bool)
            and isinstance(minimum, int)
            and not isinstance(minimum, bool)
            and stack_words > minimum
            and isinstance(age, (int, float))
            and not isinstance(age, bool)
            and math.isfinite(age)
            and age >= 0
            and isinstance(stale_after, (int, float))
            and not isinstance(stale_after, bool)
            and math.isfinite(stale_after)
            and stale_after > 0
            and age <= stale_after
        )
    return False


class EmbeddedCheckpointGate:
    """Latch policy requests until the condition clears and a later cycle is eligible."""

    def __init__(self) -> None:
        self._states: dict[tuple[str, str | None], dict[str, bool]] = {}

    def _state(
        self, task_id: str, run_id: str | None
    ) -> dict[str, bool]:
        return self._states.setdefault(
            (task_id, run_id),
            {"armed": True, "pending": False, "waiting_for_clear": False},
        )

    def observe_event(self, event: dict[str, Any]) -> None:
        name = event.get("event")
        task_id = event.get("task_id", "sensor-sampler")
        if not isinstance(task_id, str) or task_id not in TASK_CHECKPOINT_IDS:
            return
        run_id = event.get(RUN_ID_FIELD)
        if run_id is not None and not isinstance(run_id, str):
            return
        state = self._state(task_id, run_id)
        if name == "checkpoint_requested":
            state["pending"] = True
        elif name in {"checkpoint_saved", "checkpoint_failed"}:
            state["pending"] = False
            state["waiting_for_clear"] = True
            state["armed"] = False

    def should_request(
        self,
        response: dict[str, Any],
        task_id: str = "sensor-sampler",
        run_id: str | None = None,
    ) -> bool:
        if not isinstance(task_id, str) or task_id not in TASK_CHECKPOINT_IDS:
            return False
        if run_id is None:
            for assessment in response.get("assessments", []):
                if isinstance(assessment, dict) and assessment.get("task_id") == task_id:
                    observed = assessment.get("observed_inputs")
                    if isinstance(observed, dict) and isinstance(observed.get(RUN_ID_FIELD), str):
                        run_id = observed[RUN_ID_FIELD]
                    break
        state = self._state(task_id, run_id)
        recommends = _assessment_recommends_checkpoint(response, task_id)
        if state["waiting_for_clear"] and _assessment_is_normal_and_fresh(
            response, task_id
        ):
            state["armed"] = True
            state["waiting_for_clear"] = False
        if recommends and state["armed"] and not state["pending"]:
            state["pending"] = True
            state["armed"] = False
            return True
        return False


def run_bridge(
    firmware: Path,
    api_url: str,
    qemu: str = "qemu-system-riscv32",
    shutdown_timeout: float = 5.0,
    inject_stack_pressure_task: str | None = None,
    stop_requested: Callable[[], bool] | None = None,
    opener: Callable[..., Any] = urlopen,
) -> int:
    firmware = firmware.expanduser().resolve()
    if not firmware.is_file():
        raise EmbeddedBridgeError(f"firmware file does not exist: {firmware}")
    parsed_url = urlsplit(api_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise EmbeddedBridgeError("api_url must be an absolute HTTP(S) URL")
    if not math.isfinite(shutdown_timeout) or shutdown_timeout <= 0:
        raise EmbeddedBridgeError("shutdown_timeout must be a positive finite value")
    if inject_stack_pressure_task not in {None, "sensor-sampler"}:
        raise EmbeddedBridgeError(
            "stack-pressure injection is supported only for sensor-sampler"
        )

    command = [
        qemu,
        "-machine", "virt",
        "-net", "none",
        "-serial", "stdio",
        "-monitor", "none",
        "-display", "none",
        "-bios", "none",
        "-smp", "1",
        "-kernel", str(firmware),
    ]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            bufsize=0,
        )
    except OSError as error:
        raise EmbeddedBridgeError(f"could not start QEMU: {error}") from error
    if process.stdout is None or process.stdin is None:
        process.terminate()
        process.wait(timeout=shutdown_timeout)
        raise EmbeddedBridgeError("QEMU serial input/output pipe is unavailable")

    session_id = str(uuid.uuid4())
    print(f"QEMU started; bridge_session_id={session_id}", file=sys.stderr, flush=True)
    telemetry_url = urlsplit(api_url)
    if not telemetry_url.path.endswith("/api/embedded/telemetry"):
        process.terminate()
        process.wait(timeout=shutdown_timeout)
        raise EmbeddedBridgeError(
            "api_url must end with /api/embedded/telemetry for risk control"
        )
    endpoint_root = telemetry_url.path[: -len("/telemetry")]
    risk_url = telemetry_url._replace(path=f"{endpoint_root}/risk").geturl()
    event_url = telemetry_url._replace(path=f"{endpoint_root}/events").geturl()
    command_url = telemetry_url._replace(
        path=f"{endpoint_root}/bridge/command"
    ).geturl()
    selector = selectors.DefaultSelector()
    buffer = bytearray()
    dropping_oversized_line = False
    checkpoint_gate = EmbeddedCheckpointGate()
    initial_task_ids: set[str] = set()
    fault_injection_sent = False
    cli_run_id = str(uuid.uuid4())
    try:
        file_descriptor = process.stdout.fileno()
        os.set_blocking(file_descriptor, False)
        selector.register(file_descriptor, selectors.EVENT_READ)
        while stop_requested is None or not stop_requested():
            ready = selector.select(timeout=0.25)
            if not ready:
                if process.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(file_descriptor, 4096)
            except BlockingIOError:
                continue
            if not chunk:
                break
            for byte in chunk:
                if dropping_oversized_line:
                    if byte == 10:
                        dropping_oversized_line = False
                    continue
                buffer.append(byte)
                if len(buffer) > MAX_UART_LINE_BYTES:
                    print("Discarding oversized UART line", file=sys.stderr, flush=True)
                    buffer.clear()
                    dropping_oversized_line = True
                    continue
                if byte != 10:
                    continue
                line = bytes(buffer)
                buffer.clear()
                try:
                    record = parse_uart_record(line, include_events=True)
                except EmbeddedTelemetryError as error:
                    print(f"Discarding invalid UART record: {error}", file=sys.stderr, flush=True)
                    continue
                if record is None:
                    continue
                if record.get("message_type") == "task_event":
                    accepted_event = _post_task_event(
                        event_url, session_id, record, opener
                    )
                    if accepted_event.get("accepted") is False:
                        print(
                            "Ignoring stale embedded event "
                            f"{record['event']} task={record['task_id']} "
                            f"run_id={record.get(RUN_ID_FIELD)}",
                            file=sys.stderr,
                            flush=True,
                        )
                        continue
                    event_name = record["event"]
                    print(
                        f"Embedded event {event_name} task={record['task_id']}",
                        file=sys.stderr,
                        flush=True,
                    )
                    checkpoint_gate.observe_event(record)
                    continue

                result = _post_telemetry(api_url, session_id, record, opener)
                if result.get("accepted") is False:
                    print(
                        "Ignoring stale embedded telemetry "
                        f"task={record['task_id']} run_id={record.get(RUN_ID_FIELD)}",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue
                if result.get("duplicate") is True:
                    print(
                        f"Duplicate ignored: {record['task_id']} execution={record['execution_count']}",
                        file=sys.stderr,
                        flush=True,
                    )
                else:
                    print(
                        f"Forwarded {record['task_id']} execution={record['execution_count']} "
                        f"tick={record['tick_count']}",
                        file=sys.stderr,
                        flush=True,
                    )
                risk = _get_embedded_risk(risk_url, opener)
                initial_task_ids.add(record["task_id"])
                uart_command = _get_embedded_uart_command(
                    command_url, session_id, opener
                )
                if uart_command is not None:
                    try:
                        process.stdin.write(
                            f"{uart_command['command']}\n".encode("ascii")
                        )
                        process.stdin.flush()
                    except (BrokenPipeError, OSError) as error:
                        raise EmbeddedBridgeError(
                            "could not send dashboard-requested embedded fault "
                            f"over QEMU UART: {error}"
                        ) from error
                    fault_injection_sent = True
                    print(
                        "UART embedded fault command dispatched from dashboard "
                        f"request run_id={uart_command['run_id']}: "
                        f"{uart_command['command']}",
                        file=sys.stderr,
                        flush=True,
                    )
                if (
                    inject_stack_pressure_task is not None
                    and not fault_injection_sent
                    and initial_task_ids >= {"sensor-sampler", "control-loop"}
                    and _assessments_are_normal_and_fresh(risk)
                ):
                    initial_assessments = risk["assessments"]
                    for assessment in initial_assessments:
                        inputs = assessment["observed_inputs"]
                        print(
                            "Initial embedded assessment "
                            f"task={assessment['task_id']} "
                            f"risk={assessment['risk_category']} "
                            f"stack={inputs['stack_high_water_mark_words']} words",
                            file=sys.stderr,
                            flush=True,
                        )
                    command = (
                        f"INJECT_STACK_PRESSURE {inject_stack_pressure_task} "
                        f"{cli_run_id}\n"
                    ).encode("ascii")
                    try:
                        process.stdin.write(command)
                        process.stdin.flush()
                    except (BrokenPipeError, OSError) as error:
                        raise EmbeddedBridgeError(
                            f"could not send stack-pressure injection over QEMU UART: {error}"
                        ) from error
                    fault_injection_sent = True
                    print(
                        "UART fault injection sent: bounded stack pressure "
                        f"task={inject_stack_pressure_task}",
                        file=sys.stderr,
                        flush=True,
                    )
                if (
                    checkpoint_gate.should_request(
                        risk,
                        record["task_id"],
                        record.get(RUN_ID_FIELD),
                    )
                ):
                    task_run_id = record.get(RUN_ID_FIELD)
                    suffix = f" {task_run_id}" if task_run_id is not None else ""
                    command = f"CHECKPOINT {record['task_id']}{suffix}\n".encode(
                        "ascii"
                    )
                    try:
                        process.stdin.write(command)
                        process.stdin.flush()
                    except (BrokenPipeError, OSError) as error:
                        raise EmbeddedBridgeError(
                            f"could not send checkpoint request over QEMU UART: {error}"
                        ) from error
                    print(
                        f"UART checkpoint command sent for {record['task_id']} after embedded policy recommendation",
                        file=sys.stderr,
                        flush=True,
                    )
        if buffer:
            print("Discarding incomplete UART record at stream end", file=sys.stderr, flush=True)
        if dropping_oversized_line:
            print("Discarding oversized UART record at stream end", file=sys.stderr, flush=True)
        return_code = process.poll()
        if return_code is None and stop_requested is not None and stop_requested():
            return_code = 0
        elif return_code is None:
            raise EmbeddedBridgeError("QEMU UART stream disconnected while QEMU was running")
        elif return_code != 0:
            raise EmbeddedBridgeError(f"QEMU exited with status {return_code}")
        return return_code
    finally:
        selector.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=shutdown_timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=shutdown_timeout)
        process.stdout.close()
        process.stdin.close()


def _assessments_are_normal_and_fresh(response: dict[str, Any]) -> bool:
    if response.get("assessment_type") != "heuristic_operational_risk":
        return False
    assessments = response.get("assessments")
    if not isinstance(assessments, list):
        return False
    by_task = {
        assessment.get("task_id"): assessment
        for assessment in assessments
        if isinstance(assessment, dict)
    }
    return all(
        _assessment_entry_is_normal_and_fresh(by_task, task_id)
        for task_id in ("sensor-sampler", "control-loop")
    )


def _assessment_entry_is_normal_and_fresh(
    assessments: dict[str, dict[str, Any]], task_id: str
) -> bool:
    assessment = assessments.get(task_id)
    if assessment is None:
        return False
    observed = assessment.get("observed_inputs")
    configuration = assessment.get("policy_configuration")
    if (
        assessment.get("assessment_type") != "heuristic_operational_risk"
        or assessment.get("policy_id") != "embedded-stack-reserve-v1"
        or assessment.get("target") != TARGET
        or assessment.get("risk_level") != "normal"
        or assessment.get("recommendation") != "none"
        or assessment.get("matched_rules") != []
        or not isinstance(observed, dict)
        or not isinstance(configuration, dict)
    ):
        return False
    stack_words = observed.get("stack_high_water_mark_words")
    age = observed.get("telemetry_age_seconds")
    minimum = configuration.get("minimum_stack_high_water_words")
    stale_after = configuration.get("stale_after_seconds")
    return (
        observed.get("task_state") == "running"
        and isinstance(stack_words, int)
        and not isinstance(stack_words, bool)
        and isinstance(minimum, int)
        and not isinstance(minimum, bool)
        and stack_words > minimum
        and isinstance(age, (int, float))
        and not isinstance(age, bool)
        and math.isfinite(age)
        and 0 <= age <= stale_after
    )
