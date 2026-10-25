from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import selectors
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Callable, Sequence

import psutil

from twincoreos.digital_twin import DigitalTwinEngine
from twincoreos.checkpoint_manager import CheckpointError, CheckpointManager
from twincoreos.risk_predictor import BaselineRiskPredictor


class WorkerStartupError(RuntimeError):
    """A worker failed to become ready."""


class WorkerExitedError(RuntimeError):
    """A worker exited while telemetry was being collected."""

    def __init__(self, message: str, worker_id: str, pid: int) -> None:
        self.worker_id = worker_id
        self.pid = pid
        super().__init__(message)


class TelemetryCollectionError(RuntimeError):
    """A fatal error prevented telemetry collection."""


class MonitoringInterrupted(Exception):
    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(f"Monitoring interrupted by signal {signum}")


def _note_cleanup_failure(
    original_error: BaseException,
    cleanup_error: Exception,
) -> None:
    note = f"Worker cleanup also failed: {cleanup_error}"
    add_note = getattr(original_error, "add_note", None)
    if add_note is not None:
        add_note(note)
    else:
        original_error.args = (*original_error.args, note)


def cpu_utilization_percent(
    previous_cpu_seconds: float,
    current_cpu_seconds: float,
    elapsed_seconds: float,
) -> float:
    if elapsed_seconds <= 0:
        raise ValueError("elapsed_seconds must be greater than zero")
    if current_cpu_seconds < previous_cpu_seconds:
        raise ValueError("cumulative CPU time cannot decrease")
    return (current_cpu_seconds - previous_cpu_seconds) / elapsed_seconds * 100.0


@dataclass
class Worker:
    index: int
    child: subprocess.Popen[bytes]
    process: psutil.Process
    process_created_at_utc: str
    workload_started_monotonic: float
    previous_cpu_seconds: float
    previous_cpu_sample_monotonic: float
    control_lock: threading.Lock = field(default_factory=threading.Lock)
    worker_id: str | None = None

    @property
    def task_id(self) -> str:
        return self.worker_id or f"worker-{self.index}"

    def request(self, command: dict[str, Any], timeout: float = 5.0) -> dict[str, Any]:
        if self.child.stdin is None or self.child.stdout is None:
            raise RuntimeError(f"Worker {self.index} control channel is unavailable")
        request_id = uuid.uuid4().hex
        payload = {**command, "request_id": request_id}
        with self.control_lock:
            try:
                self.child.stdin.write(
                    (json.dumps(payload, separators=(",", ":")) + "\n").encode()
                )
                self.child.stdin.flush()
            except (BrokenPipeError, OSError) as error:
                raise WorkerExitedError(
                    f"Worker {self.index} control channel closed",
                    self.task_id,
                    self.child.pid,
                ) from error

            with selectors.DefaultSelector() as selector:
                selector.register(self.child.stdout, selectors.EVENT_READ)
                if not selector.select(timeout):
                    raise TimeoutError(
                        f"Timed out waiting for worker {self.index} control response"
                    )
            line = self.child.stdout.readline()
            if not line:
                raise WorkerExitedError(
                    f"Worker {self.index} exited before its control response",
                    self.task_id,
                    self.child.pid,
                )
            try:
                response = json.loads(line)
            except json.JSONDecodeError as error:
                raise RuntimeError(
                    f"Worker {self.index} sent invalid control JSON"
                ) from error
            if (
                not isinstance(response, dict)
                or response.get("request_id") != request_id
            ):
                raise RuntimeError(
                    f"Worker {self.index} sent a mismatched control response"
                )
            if response.get("event") == "error":
                raise RuntimeError(
                    f"Worker {self.index} rejected control request: "
                    f"{response.get('message', 'unspecified error')}"
                )
            if response.get("event") != "state" or not isinstance(
                response.get("state"), dict
            ):
                raise RuntimeError(
                    f"Worker {self.index} sent an invalid state response"
                )
            return response["state"]


def _cpu_total(cpu_times: Any) -> float:
    return cpu_times.user + cpu_times.system


class WorkerPool:
    def __init__(
        self,
        workers: int,
        memory_mib: int,
        startup_timeout: float = 10.0,
        shutdown_timeout: float = 5.0,
        worker_command: Sequence[str] | None = None,
        stop_requested: Callable[[], int | None] | None = None,
    ) -> None:
        self.workers = workers
        self.memory_mib = memory_mib
        self.startup_timeout = startup_timeout
        self.shutdown_timeout = shutdown_timeout
        self.worker_command = worker_command
        self.stop_requested = stop_requested
        self._processes: list[Worker] = []
        self._closed = False

    def __enter__(self) -> WorkerPool:
        try:
            self._start_workers()
            self._wait_until_ready()
            self._set_cpu_baselines()
        except BaseException as error:
            try:
                self.close()
            except Exception as cleanup_error:
                _note_cleanup_failure(error, cleanup_error)
            raise
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        try:
            self.close()
        except Exception as cleanup_error:
            if exc_value is None:
                raise
            _note_cleanup_failure(exc_value, cleanup_error)

    def _command(self) -> list[str]:
        if self.worker_command is not None:
            return list(self.worker_command)
        return [
            sys.executable,
            "-m",
            "twincoreos.worker",
            "--memory-mib",
            str(self.memory_mib),
        ]

    def _start_workers(self) -> None:
        for index in range(1, self.workers + 1):
            self._launch_worker(index)

    def _launch_worker(self, index: int, worker_id: str | None = None) -> Worker:
        self._check_interrupted()
        child = subprocess.Popen(
            self._command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            bufsize=0,
        )
        try:
            process = psutil.Process(child.pid)
            created_at = datetime.fromtimestamp(
                process.create_time(), timezone.utc
            ).isoformat()
        except BaseException:
            try:
                child.terminate()
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=self.shutdown_timeout)
            except subprocess.TimeoutExpired:
                try:
                    child.kill()
                except ProcessLookupError:
                    pass
                child.wait()
            if child.stdin is not None:
                child.stdin.close()
            if child.stdout is not None:
                child.stdout.close()
            raise
        worker = Worker(
            index=index,
            child=child,
            process=process,
            process_created_at_utc=created_at,
            workload_started_monotonic=0.0,
            previous_cpu_seconds=0.0,
            previous_cpu_sample_monotonic=0.0,
            worker_id=worker_id,
        )
        self._processes.append(worker)
        return worker

    def _check_interrupted(self) -> None:
        if self.stop_requested is not None:
            signum = self.stop_requested()
            if signum is not None:
                raise MonitoringInterrupted(signum)

    def _wait_until_ready(self, workers: Sequence[Worker] | None = None) -> None:
        deadline = time.monotonic() + self.startup_timeout
        selected = tuple(workers) if workers is not None else tuple(self._processes)
        pending = {worker.child.stdout: worker for worker in selected}
        with selectors.DefaultSelector() as selector:
            for stream in pending:
                if stream is None:
                    raise WorkerStartupError("worker readiness pipe is unavailable")
                selector.register(stream, selectors.EVENT_READ)

            while pending:
                self._check_interrupted()
                for worker in selected:
                    if worker.child.poll() is not None and worker.child.stdout in pending:
                        raise WorkerStartupError(
                            f"Worker {worker.index} (pid {worker.child.pid}) exited "
                            f"during startup with status {worker.child.returncode}"
                        )

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    workers = ", ".join(
                        f"{worker.index} (pid {worker.child.pid})"
                        for worker in pending.values()
                    )
                    raise WorkerStartupError(
                        f"Timed out after {self.startup_timeout:g}s waiting for "
                        f"worker readiness: {workers}"
                    )

                for key, _events in selector.select(min(remaining, 0.1)):
                    worker = pending[key.fileobj]
                    line = key.fileobj.readline()
                    selector.unregister(key.fileobj)
                    del pending[key.fileobj]
                    if not line:
                        raise WorkerStartupError(
                            f"Worker {worker.index} (pid {worker.child.pid}) closed "
                            "its readiness pipe without a ready message"
                        )
                    try:
                        message = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError) as error:
                        raise WorkerStartupError(
                            f"Worker {worker.index} sent an invalid readiness message"
                        ) from error
                    if not isinstance(message, dict):
                        raise WorkerStartupError(
                            f"Worker {worker.index} sent an invalid readiness message"
                        )
                    if message.get("event") == "error":
                        raise WorkerStartupError(
                            f"Worker {worker.index} initialization failed: "
                            f"{message.get('error_type', 'Error')}: "
                            f"{message.get('message', 'no details provided')}"
                        )
                    if (
                        message.get("event") != "ready"
                        or not isinstance(message.get("workload_started_monotonic"), (int, float))
                    ):
                        raise WorkerStartupError(
                            f"Worker {worker.index} sent an invalid readiness message"
                        )
                    worker.workload_started_monotonic = float(
                        message["workload_started_monotonic"]
                    )

    def _set_cpu_baselines(self) -> None:
        now = time.monotonic()
        for worker in self._processes:
            self._check_interrupted()
            try:
                worker.previous_cpu_seconds = _cpu_total(worker.process.cpu_times())
            except psutil.NoSuchProcess as error:
                raise WorkerExitedError(
                    f"Worker {worker.index} (pid {worker.child.pid}) exited "
                    "before CPU baseline collection",
                    worker.task_id,
                    worker.child.pid,
                ) from error
            except psutil.Error as error:
                raise TelemetryCollectionError(
                    f"Could not establish CPU baseline for worker {worker.index} "
                    f"(pid {worker.child.pid}): {error}"
                ) from error
            worker.previous_cpu_sample_monotonic = now

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        errors: list[Exception] = []
        for worker in self._processes:
            if worker.child.poll() is None:
                try:
                    worker.child.terminate()
                except ProcessLookupError:
                    pass
                except OSError as error:
                    errors.append(error)

        for worker in self._processes:
            try:
                worker.child.wait(timeout=self.shutdown_timeout)
            except subprocess.TimeoutExpired:
                try:
                    worker.child.kill()
                except ProcessLookupError:
                    pass
                try:
                    worker.child.wait()
                except OSError as error:
                    errors.append(error)
            except OSError as error:
                errors.append(error)
            try:
                if worker.child.stdin is not None:
                    worker.child.stdin.close()
                if worker.child.stdout is not None:
                    worker.child.stdout.close()
            except OSError as error:
                errors.append(error)

        if errors:
            raise RuntimeError(f"Worker shutdown failed: {errors[0]}") from errors[0]

    def sample(self, monitor_started_monotonic: float) -> list[dict[str, object]]:
        samples: list[dict[str, object]] = []
        for worker in self._processes:
            if worker.child.poll() is not None:
                raise WorkerExitedError(
                    f"Worker {worker.index} (pid {worker.child.pid}) exited "
                    f"unexpectedly with status {worker.child.returncode}",
                    worker.task_id,
                    worker.child.pid,
                )

            measurement_errors: dict[str, str] = {}
            try:
                now = time.monotonic()
                cpu_times = worker.process.cpu_times()
                cpu_total_seconds = _cpu_total(cpu_times)
                cpu_interval_seconds = now - worker.previous_cpu_sample_monotonic
                cpu_utilization = cpu_utilization_percent(
                    worker.previous_cpu_seconds,
                    cpu_total_seconds,
                    cpu_interval_seconds,
                )
                worker.previous_cpu_seconds = cpu_total_seconds
                worker.previous_cpu_sample_monotonic = now
                user_cpu_seconds: float | None = cpu_times.user
                system_cpu_seconds: float | None = cpu_times.system
            except psutil.NoSuchProcess as error:
                raise WorkerExitedError(
                    f"Worker {worker.index} (pid {worker.child.pid}) exited during "
                    "CPU measurement",
                    worker.task_id,
                    worker.child.pid,
                ) from error
            except psutil.AccessDenied as error:
                measurement_errors["cpu"] = f"{type(error).__name__}: {error}"
                user_cpu_seconds = None
                system_cpu_seconds = None
                cpu_total_seconds = None
                cpu_interval_seconds = None
                cpu_utilization = None
            except psutil.Error as error:
                raise TelemetryCollectionError(
                    f"Fatal CPU collection error for worker {worker.index} "
                    f"(pid {worker.child.pid}): {error}"
                ) from error

            try:
                memory_rss_bytes: int | None = worker.process.memory_info().rss
            except psutil.NoSuchProcess as error:
                raise WorkerExitedError(
                    f"Worker {worker.index} (pid {worker.child.pid}) exited during "
                    "memory measurement",
                    worker.task_id,
                    worker.child.pid,
                ) from error
            except psutil.AccessDenied as error:
                measurement_errors["memory_rss_bytes"] = (
                    f"{type(error).__name__}: {error}"
                )
                memory_rss_bytes = None
            except psutil.Error as error:
                raise TelemetryCollectionError(
                    f"Fatal memory collection error for worker {worker.index} "
                    f"(pid {worker.child.pid}): {error}"
                ) from error

            try:
                process_status: str | None = worker.process.status()
            except psutil.NoSuchProcess as error:
                raise WorkerExitedError(
                    f"Worker {worker.index} (pid {worker.child.pid}) exited during "
                    "status measurement",
                    worker.task_id,
                    worker.child.pid,
                ) from error
            except psutil.AccessDenied as error:
                measurement_errors["process_status"] = (
                    f"{type(error).__name__}: {error}"
                )
                process_status = None
            except psutil.Error as error:
                raise TelemetryCollectionError(
                    f"Fatal status collection error for worker {worker.index} "
                    f"(pid {worker.child.pid}): {error}"
                ) from error

            try:
                parent_pid: int | None = worker.process.ppid()
            except psutil.NoSuchProcess as error:
                raise WorkerExitedError(
                    f"Worker {worker.index} (pid {worker.child.pid}) exited during "
                    "parent PID measurement",
                    worker.task_id,
                    worker.child.pid,
                ) from error
            except psutil.AccessDenied as error:
                measurement_errors["ppid"] = f"{type(error).__name__}: {error}"
                parent_pid = None
            except psutil.Error as error:
                raise TelemetryCollectionError(
                    f"Fatal parent PID collection error for worker {worker.index} "
                    f"(pid {worker.child.pid}): {error}"
                ) from error

            captured_at_utc = datetime.now(timezone.utc).isoformat()
            captured_monotonic = time.monotonic()
            samples.append(
                {
                    "type": "telemetry",
                    "task_id": worker.task_id,
                    "pid": worker.child.pid,
                    "ppid": parent_pid,
                    "process_created_at_utc": worker.process_created_at_utc,
                    "captured_at_utc": captured_at_utc,
                    "monitor_elapsed_seconds": max(
                        0.0, captured_monotonic - monitor_started_monotonic
                    ),
                    "workload_elapsed_seconds": max(
                        0.0,
                        captured_monotonic - worker.workload_started_monotonic,
                    ),
                    "cpu_time_user_seconds_total": user_cpu_seconds,
                    "cpu_time_system_seconds_total": system_cpu_seconds,
                    "cpu_time_total_seconds": cpu_total_seconds,
                    "cpu_utilization_percent_one_core": cpu_utilization,
                    "cpu_interval_seconds": cpu_interval_seconds,
                    "cpu_utilization_scope": "one_logical_cpu_core",
                    "memory_rss_bytes": memory_rss_bytes,
                    "process_status": process_status,
                    "measurement_errors": measurement_errors,
                }
            )
        return samples

    def application_state(self, worker_id: str) -> tuple[int, dict[str, Any]]:
        worker = next(
            (item for item in self._processes if item.task_id == worker_id),
            None,
        )
        if worker is None:
            raise KeyError(f"unknown worker identity: {worker_id}")
        return worker.child.pid, worker.request({"command": "snapshot"})

    def active_worker_ids(self) -> tuple[str, ...]:
        return tuple(
            worker.task_id
            for worker in self._processes
            if worker.child.poll() is None
        )

    def restore_application_state(
        self,
        worker_id: str,
        state: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        worker = next(
            (item for item in self._processes if item.task_id == worker_id),
            None,
        )
        if worker is None:
            raise KeyError(f"unknown worker identity: {worker_id}")
        return worker.child.pid, worker.request(
            {"command": "restore", "state": state}
        )

    def advance_application_state(
        self,
        worker_id: str,
        steps: int,
    ) -> tuple[int, dict[str, Any]]:
        worker = next(
            (item for item in self._processes if item.task_id == worker_id),
            None,
        )
        if worker is None:
            raise KeyError(f"unknown worker identity: {worker_id}")
        return worker.child.pid, worker.request(
            {"command": "advance", "steps": steps}
        )

    def inject_termination(self, worker_id: str) -> tuple[Worker, int]:
        worker = next(
            (item for item in self._processes if item.task_id == worker_id),
            None,
        )
        if worker is None:
            raise KeyError(f"unknown active worker identity: {worker_id}")
        pid = worker.child.pid
        if worker.child.poll() is None:
            worker.child.kill()
        try:
            worker.child.wait(timeout=self.shutdown_timeout)
        except subprocess.TimeoutExpired as error:
            raise TimeoutError(
                f"target worker {worker_id} did not terminate after kill"
            ) from error
        self._processes.remove(worker)
        if worker.child.stdin is not None:
            worker.child.stdin.close()
        if worker.child.stdout is not None:
            worker.child.stdout.close()
        return worker, pid

    def start_replacement(self, worker_id: str) -> Worker:
        if any(worker.task_id == worker_id for worker in self._processes):
            raise ValueError(f"worker identity is already active: {worker_id}")
        index = max((worker.index for worker in self._processes), default=0) + 1
        replacement = self._launch_worker(index, worker_id=worker_id)
        try:
            self._wait_until_ready([replacement])
            self._set_cpu_baselines_for([replacement])
        except BaseException as error:
            try:
                self._stop_worker(replacement)
            except Exception as cleanup_error:
                _note_cleanup_failure(error, cleanup_error)
            if replacement in self._processes:
                self._processes.remove(replacement)
            raise
        return replacement

    def _set_cpu_baselines_for(self, workers: Sequence[Worker]) -> None:
        now = time.monotonic()
        for worker in workers:
            try:
                worker.previous_cpu_seconds = _cpu_total(worker.process.cpu_times())
            except psutil.NoSuchProcess as error:
                raise WorkerExitedError(
                    f"Worker {worker.task_id} exited before CPU baseline collection",
                    worker.task_id,
                    worker.child.pid,
                ) from error
            except psutil.Error as error:
                raise TelemetryCollectionError(
                    f"Could not establish CPU baseline for {worker.task_id}: {error}"
                ) from error
            worker.previous_cpu_sample_monotonic = now

    def _stop_worker(self, worker: Worker) -> None:
        if worker.child.poll() is None:
            try:
                worker.child.terminate()
            except ProcessLookupError:
                pass
        try:
            worker.child.wait(timeout=self.shutdown_timeout)
        except subprocess.TimeoutExpired:
            try:
                worker.child.kill()
            except ProcessLookupError:
                pass
            worker.child.wait()
        if worker.child.stdin is not None:
            worker.child.stdin.close()
        if worker.child.stdout is not None:
            worker.child.stdout.close()


def monitor(
    workers: int,
    duration: float,
    sample_interval: float,
    memory_mib: int,
    startup_timeout: float = 10.0,
    shutdown_timeout: float = 5.0,
    stop_requested: Callable[[], int | None] | None = None,
    stop_event: threading.Event | None = None,
    checkpoint_interval: float = 5.0,
    checkpoint_budget_bytes: int = 10 * 1024 * 1024,
    checkpoint_dir: str | Path = ".twincoreos/checkpoints",
    fault_worker_id: str | None = None,
) -> None:
    twin_engine = DigitalTwinEngine(
        stale_after_seconds=max(3.0 * sample_interval, sample_interval + 0.001)
    )
    risk_predictor = BaselineRiskPredictor(
        assessment_validity_seconds=max(3.0 * sample_interval, sample_interval + 0.001)
    )
    checkpoint_manager = CheckpointManager(
        checkpoint_dir,
        max_storage_bytes=checkpoint_budget_bytes,
    )
    with WorkerPool(
        workers,
        memory_mib,
        startup_timeout=startup_timeout,
        shutdown_timeout=shutdown_timeout,
        stop_requested=stop_requested,
    ) as pool:
        monitor_started = time.monotonic()
        deadline = monitor_started + duration
        next_sample = monitor_started + sample_interval
        next_checkpoint = monitor_started + checkpoint_interval
        fault_injected = False
        while next_sample <= deadline:
            pool._check_interrupted()
            wait_seconds = max(0.0, next_sample - time.monotonic())
            if stop_event is not None:
                stop_event.wait(wait_seconds)
            else:
                time.sleep(wait_seconds)
            pool._check_interrupted()
            try:
                samples = pool.sample(monitor_started)
            except WorkerExitedError as error:
                twin_engine.mark_failed(
                    error.worker_id,
                    error.pid,
                    str(error),
                )
                print(
                    json.dumps(
                        twin_engine.snapshot(),
                        separators=(",", ":"),
                    ),
                    flush=True,
                )
                for twin in twin_engine.states():
                    print(
                        json.dumps(
                            risk_predictor.assess(twin).to_dict(),
                            separators=(",", ":"),
                        ),
                        flush=True,
                    )
                raise
            for sample in samples:
                twin_engine.update(sample)
                print(json.dumps(sample, separators=(",", ":")), flush=True)
            print(
                json.dumps(twin_engine.snapshot(), separators=(",", ":")),
                flush=True,
            )
            for twin in twin_engine.states():
                print(
                    json.dumps(
                        risk_predictor.assess(twin).to_dict(),
                        separators=(",", ":"),
                    ),
                    flush=True,
                )
            if fault_worker_id is not None and not fault_injected:
                from twincoreos.recovery import RecoveryCoordinator

                worker_pid, application_state = pool.application_state(fault_worker_id)
                _pid, application_state = pool.advance_application_state(
                    fault_worker_id, 1
                )
                checkpoint_manager.create_checkpoint(
                    fault_worker_id,
                    worker_pid,
                    application_state,
                )
                for checkpoint_event in checkpoint_manager.drain_events():
                    print(
                        json.dumps(checkpoint_event, separators=(",", ":")),
                        flush=True,
                    )
                recovery = RecoveryCoordinator(pool, twin_engine, checkpoint_manager)
                try:
                    result = recovery.recover(fault_worker_id)
                finally:
                    for event in recovery.drain_events():
                        print(json.dumps(event, separators=(",", ":")), flush=True)
                    for event in checkpoint_manager.drain_events():
                        print(json.dumps(event, separators=(",", ":")), flush=True)
                print(
                    json.dumps(
                        {
                            "type": "recovery_result",
                            **result,
                            "twin_snapshot": twin_engine.snapshot(),
                        },
                        separators=(",", ":"),
                    ),
                    flush=True,
                )
                fault_injected = True
            if time.monotonic() >= next_checkpoint:
                for worker_twin in twin_engine.states():
                    try:
                        worker_pid, application_state = pool.application_state(
                            worker_twin.worker_id
                        )
                        checkpoint_manager.create_checkpoint(
                            worker_twin.worker_id,
                            worker_pid,
                            application_state,
                        )
                    except CheckpointError:
                        pass
                    for checkpoint_event in checkpoint_manager.drain_events():
                        print(
                            json.dumps(checkpoint_event, separators=(",", ":")),
                            flush=True,
                        )
                    print(
                        json.dumps(
                            {
                                "type": "checkpoint_status",
                                "storage_usage_bytes": (
                                    checkpoint_manager.storage_usage_bytes()
                                ),
                                "storage_budget_bytes": checkpoint_budget_bytes,
                            },
                            separators=(",", ":"),
                        ),
                        flush=True,
                    )
                next_checkpoint = time.monotonic() + checkpoint_interval
            next_sample += sample_interval
