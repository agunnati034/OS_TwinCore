"""Dependency-free JSON HTTP API for the live TwinCoreOS monitor."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

from twincoreos.embedded_telemetry import EmbeddedTelemetryError
from twincoreos.runtime import (
    EmbeddedFaultRequestError,
    EmbeddedTelemetryConflictError,
    EmbeddedTelemetryOutOfOrderError,
    MonitorRuntime,
    RecoveryRequestError,
    RuntimeUnavailableError,
    UnknownWorkerError,
)


class TwinCoreHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], runtime: MonitorRuntime) -> None:
        self.runtime = runtime
        super().__init__(address, TwinCoreRequestHandler)


class TwinCoreRequestHandler(BaseHTTPRequestHandler):
    server: TwinCoreHTTPServer
    protocol_version = "HTTP/1.1"
    allowed_origins = {
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    }

    def handle(self) -> None:
        try:
            super().handle()
        except ConnectionError:
            self.close_connection = True

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        origin = self.headers.get("Origin")
        if origin in self.allowed_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as error:
            raise ValueError("Content-Length must be an integer") from error
        if length < 0 or length > 4096:
            raise ValueError("request body must be at most 4096 bytes")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("request body must be valid JSON") from error
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def do_OPTIONS(self) -> None:
        origin = self.headers.get("Origin")
        if origin not in self.allowed_origins:
            self._send_json(403, {"error": "origin is not allowed"})
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        snapshot = self.server.runtime.snapshot()
        if path == "/api/dashboard":
            self._send_json(
                200,
                {
                    "status": {
                        "status": snapshot["status"],
                        "error": snapshot["error"],
                        "started_at_utc": snapshot["started_at_utc"],
                        "configured_worker_count": snapshot["configured_worker_count"],
                        "active_worker_count": snapshot["active_worker_count"],
                        "sample_interval_seconds": snapshot["sample_interval_seconds"],
                    },
                    "workers": snapshot["workers"],
                    "telemetry": {
                        "latest": [
                            worker["telemetry"]
                            for worker in snapshot["workers"]
                            if worker["telemetry"] is not None
                        ],
                        "history": snapshot["history"],
                    },
                    "twins": snapshot["digital_twin"],
                    "risks": {"assessments": snapshot["risk_assessments"]},
                    "checkpoints": {
                        "checkpoints": snapshot["checkpoints"],
                        "storage": snapshot["checkpoint_status"],
                    },
                    "events": {"events": snapshot["events"]},
                    "recovery": {"recovery": snapshot["recovery"]},
                    "embedded_tasks": snapshot["embedded_tasks"],
                    "embedded_checkpoint_status": snapshot[
                        "embedded_checkpoint_status"
                    ],
                    "embedded_checkpoint_storage": snapshot[
                        "embedded_checkpoint_storage"
                    ],
                    "embedded_fault_injection": snapshot[
                        "embedded_fault_injection"
                    ],
                    "embedded_fault_runs": snapshot["embedded_fault_runs"],
                    "embedded_risk_assessments": snapshot[
                        "embedded_risk_assessments"
                    ],
                    "embedded_risk_events": snapshot["embedded_risk_events"],
                    "embedded_ml": snapshot["embedded_ml"],
                },
            )
        elif path in {"/api", "/api/status"}:
            self._send_json(
                200,
                {
                    "status": snapshot["status"],
                    "error": snapshot["error"],
                    "started_at_utc": snapshot["started_at_utc"],
                    "configured_worker_count": snapshot["configured_worker_count"],
                    "active_worker_count": snapshot["active_worker_count"],
                    "sample_interval_seconds": snapshot["sample_interval_seconds"],
                },
            )
        elif path == "/api/workers":
            self._send_json(200, {"workers": snapshot["workers"]})
        elif path == "/api/telemetry":
            self._send_json(
                200,
                {
                    "latest": [
                        worker["telemetry"]
                        for worker in snapshot["workers"]
                        if worker["telemetry"] is not None
                    ],
                    "history": snapshot["history"],
                    "sample_interval_seconds": snapshot["sample_interval_seconds"],
                },
            )
        elif path == "/api/twins":
            self._send_json(200, snapshot["digital_twin"])
        elif path == "/api/risk":
            self._send_json(200, {"assessments": snapshot["risk_assessments"]})
        elif path == "/api/checkpoints":
            self._send_json(
                200,
                {
                    "checkpoints": snapshot["checkpoints"],
                    "storage": snapshot["checkpoint_status"],
                },
            )
        elif path == "/api/events":
            try:
                values = parse_qs(parsed.query, strict_parsing=True)
                raw_limit = values.get("limit", ["100"])[0]
                if len(values) > 1 or any(key != "limit" for key in values):
                    raise ValueError("only the limit query parameter is supported")
                limit = int(raw_limit)
                self._send_json(200, {"events": self.server.runtime.events(limit)})
            except ValueError as error:
                self._send_json(400, {"error": str(error)})
        elif path == "/api/recovery":
            self._send_json(200, {"recovery": snapshot["recovery"]})
        elif path == "/api/embedded/tasks":
            self._send_json(200, snapshot["embedded_tasks"])
        elif path == "/api/embedded/risk":
            self._send_json(
                200,
                {
                    "assessment_type": "heuristic_operational_risk",
                    "assessments": snapshot["embedded_risk_assessments"],
                },
            )
        elif path == "/api/embedded/ml":
            self._send_json(200, snapshot["embedded_ml"])
        elif path == "/api/embedded/fault-injection":
            self._send_json(
                200,
                {
                    "request": snapshot["embedded_fault_injection"],
                    "runs": snapshot["embedded_fault_runs"],
                    "assessments": snapshot["embedded_risk_assessments"],
                },
            )
        elif path == "/api/embedded/bridge/command":
            try:
                values = parse_qs(parsed.query, strict_parsing=True)
                if set(values) != {"bridge_session_id"}:
                    raise ValueError(
                        "bridge_session_id is the only supported query parameter"
                    )
                bridge_session_id = values["bridge_session_id"][0]
                result = self.server.runtime.take_embedded_uart_command(
                    bridge_session_id
                )
                self._send_json(200, {"command": result})
            except (EmbeddedFaultRequestError, ValueError) as error:
                self._send_json(400, {"error": str(error)})
        else:
            self._send_json(404, {"error": "endpoint not found"})

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/embedded/fault-injection":
            try:
                body = self._body()
                if set(body) != {"task_id", "confirm"} or body["confirm"] is not True:
                    raise ValueError(
                        'request must contain {"task_id": "sensor-sampler", "confirm": true}'
                    )
                request = self.server.runtime.request_embedded_fault(
                    body["task_id"]
                )
                self._send_json(202, {"success": True, "request": request})
            except EmbeddedFaultRequestError as error:
                self._send_json(409, {"success": False, "error": str(error)})
            except RuntimeUnavailableError as error:
                self._send_json(503, {"success": False, "error": str(error)})
            except ValueError as error:
                self._send_json(400, {"success": False, "error": str(error)})
            return
        if path == "/api/embedded/telemetry":
            try:
                body = self._body()
                if set(body) != {"bridge_session_id", "telemetry"}:
                    raise ValueError(
                        "request must contain bridge_session_id and telemetry"
                    )
                result = self.server.runtime.ingest_embedded_telemetry(
                    body["bridge_session_id"], body["telemetry"]
                )
                self._send_json(200, result)
            except (
                EmbeddedTelemetryConflictError,
                EmbeddedTelemetryOutOfOrderError,
            ) as error:
                self._send_json(
                    409, {"success": False, "error": str(error)}
                )
            except (EmbeddedTelemetryError, ValueError) as error:
                self._send_json(400, {"success": False, "error": str(error)})
            except RuntimeUnavailableError as error:
                self._send_json(503, {"success": False, "error": str(error)})
            return
        if path == "/api/embedded/events":
            try:
                body = self._body()
                if set(body) != {"bridge_session_id", "event"}:
                    raise ValueError(
                        "request must contain bridge_session_id and event"
                    )
                result = self.server.runtime.ingest_embedded_task_event(
                    body["bridge_session_id"], body["event"]
                )
                self._send_json(200, result)
            except (EmbeddedTelemetryError, ValueError) as error:
                self._send_json(400, {"success": False, "error": str(error)})
            except RuntimeUnavailableError as error:
                self._send_json(503, {"success": False, "error": str(error)})
            return
        prefix = "/api/workers/"
        suffix = "/fault"
        if not (path.startswith(prefix) and path.endswith(suffix)):
            self._send_json(404, {"error": "endpoint not found"})
            return
        worker_id = unquote(path[len(prefix) : -len(suffix)])
        if not worker_id or "/" in worker_id:
            self._send_json(400, {"error": "a valid worker ID is required"})
            return
        try:
            body = self._body()
            if set(body) != {"confirm"} or body["confirm"] is not True:
                raise ValueError('request must contain {"confirm": true}')
            result = self.server.runtime.inject_fault(worker_id)
            self._send_json(200, {"success": True, "recovery": result})
        except UnknownWorkerError as error:
            self._send_json(404, {"success": False, "error": str(error)})
        except RuntimeUnavailableError as error:
            self._send_json(503, {"success": False, "error": str(error)})
        except RecoveryRequestError as error:
            self._send_json(
                409,
                {
                    "success": False,
                    "error": str(error),
                    "events": error.events,
                },
            )
        except TimeoutError as error:
            self._send_json(504, {"success": False, "error": str(error)})
        except ValueError as error:
            self._send_json(400, {"success": False, "error": str(error)})
        except Exception as error:
            self._send_json(409, {"success": False, "error": str(error)})


def create_server(
    runtime: MonitorRuntime, host: str = "127.0.0.1", port: int = 8765
) -> TwinCoreHTTPServer:
    return TwinCoreHTTPServer((host, port), runtime)


def serve(
    runtime: MonitorRuntime,
    host: str = "127.0.0.1",
    port: int = 8765,
    stop_requested: Callable[[], bool] | None = None,
) -> None:
    runtime.start()
    server: TwinCoreHTTPServer | None = None
    try:
        server = create_server(runtime, host, port)
        bound_host, bound_port = server.server_address[:2]
        display_host = f"[{bound_host}]" if ":" in bound_host else bound_host
        print(
            f"TwinCoreOS API listening on http://{display_host}:{bound_port}",
            file=sys.stderr,
            flush=True,
        )
        server.timeout = 0.25
        while stop_requested is None or not stop_requested():
            server.handle_request()
    finally:
        if server is not None:
            server.server_close()
        runtime.stop()
