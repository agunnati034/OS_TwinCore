import argparse
from datetime import datetime, timezone
import json
import select
import signal
import sys
import time


MAX_STATE_INTEGER = 2**63 - 1


def validate_application_state(state: object) -> dict[str, int]:
    if not isinstance(state, dict) or set(state) != {"counter", "accumulator"}:
        raise ValueError("state must contain exactly counter and accumulator")
    counter = state["counter"]
    accumulator = state["accumulator"]
    if (
        not isinstance(counter, int)
        or isinstance(counter, bool)
        or not 0 <= counter <= MAX_STATE_INTEGER
    ):
        raise ValueError("counter must be a non-negative signed 64-bit integer")
    if (
        not isinstance(accumulator, int)
        or isinstance(accumulator, bool)
        or not 0 <= accumulator <= MAX_STATE_INTEGER
    ):
        raise ValueError("accumulator must be a non-negative signed 64-bit integer")
    return {"counter": counter, "accumulator": accumulator}


def _respond(message: dict[str, object]) -> None:
    print(json.dumps(message, separators=(",", ":")), flush=True)


def _handle_command(command: object, state: dict[str, int]) -> dict[str, object]:
    if not isinstance(command, dict) or not isinstance(command.get("request_id"), str):
        return {"event": "error", "message": "invalid command"}
    request_id = command["request_id"]
    operation = command.get("command")
    if operation == "snapshot":
        return {"event": "state", "request_id": request_id, "state": dict(state)}
    if operation == "restore":
        try:
            restored = validate_application_state(command.get("state"))
        except ValueError as error:
            return {"event": "error", "request_id": request_id, "message": str(error)}
        state.update(restored)
        return {"event": "state", "request_id": request_id, "state": dict(state)}
    if operation == "advance":
        steps = command.get("steps")
        if not isinstance(steps, int) or isinstance(steps, bool) or not 1 <= steps <= 100_000:
            return {
                "event": "error",
                "request_id": request_id,
                "message": "steps must be an integer from 1 to 100000",
            }
        final_counter = state["counter"] + steps
        if final_counter > MAX_STATE_INTEGER:
            return {"event": "error", "request_id": request_id, "message": "counter overflow"}
        accumulator_delta = steps * state["counter"] + steps * (steps + 1) // 2
        if state["accumulator"] + accumulator_delta > MAX_STATE_INTEGER:
            return {
                "event": "error",
                "request_id": request_id,
                "message": "accumulator overflow",
            }
        state["accumulator"] += accumulator_delta
        state["counter"] = final_counter
        return {"event": "state", "request_id": request_id, "state": dict(state)}
    return {
        "event": "error",
        "request_id": request_id,
        "message": "unsupported command",
    }


def run(memory_mib: int, initial_state: dict[str, int] | None = None) -> int:
    stopping = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    try:
        memory = bytearray(memory_mib * 1024 * 1024)
        for offset in range(0, len(memory), 4096):
            memory[offset] = offset & 0xFF

        application_state = validate_application_state(
            initial_state
            if initial_state is not None
            else {"counter": 0, "accumulator": 0}
        )
        workload_state = 1
        workload_started_monotonic = time.monotonic()
        print(
            json.dumps(
                {
                    "event": "ready",
                    "workload_started_monotonic": workload_started_monotonic,
                    "application_state_schema_version": 1,
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
    except Exception as error:
        print(
            json.dumps(
                {
                    "event": "error",
                    "error_type": type(error).__name__,
                    "message": str(error),
                    "occurred_at_utc": datetime.now(timezone.utc).isoformat(),
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
        return 1

    while not stopping:
        readable, _, _ = select.select([sys.stdin], [], [], 0)
        if readable:
            line = sys.stdin.readline()
            if not line:
                stopping = True
                continue
            try:
                command = json.loads(line)
                response = _handle_command(command, application_state)
            except json.JSONDecodeError:
                response = {"event": "error", "message": "invalid JSON command"}
            _respond(response)
        for _ in range(10_000):
            workload_state = (
                workload_state * 1_103_515_245 + 12_345
            ) & 0x7FFFFFFF
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--memory-mib", type=int, required=True)
    args = parser.parse_args()
    if args.memory_mib < 1:
        parser.error("--memory-mib must be at least 1")
    return run(args.memory_mib)


if __name__ == "__main__":
    raise SystemExit(main())
