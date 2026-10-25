import argparse
import math
import signal
import sys
import threading
from pathlib import Path

from twincoreos.api import serve
from twincoreos.embedded_telemetry import EmbeddedBridgeError, run_bridge
from twincoreos.monitor import MonitoringInterrupted, monitor
from twincoreos.recovery import RecoveryError
from twincoreos.runtime import MonitorRuntime, RuntimeUnavailableError


def main() -> int:
    parser = argparse.ArgumentParser(prog="twincoreos")
    commands = parser.add_subparsers(dest="command", required=True)
    monitor_command = commands.add_parser(
        "monitor", help="measure CPU, memory, time, and state of worker processes"
    )
    monitor_command.add_argument("--workers", type=int, default=1)
    monitor_command.add_argument("--duration", type=float, default=10.0)
    monitor_command.add_argument("--sample-interval", type=float, default=1.0)
    monitor_command.add_argument("--memory-mib", type=int, default=8)
    monitor_command.add_argument("--startup-timeout", type=float, default=10.0)
    monitor_command.add_argument("--shutdown-timeout", type=float, default=5.0)
    monitor_command.add_argument("--checkpoint-interval", type=float, default=5.0)
    monitor_command.add_argument("--checkpoint-budget-bytes", type=int, default=10 * 1024 * 1024)
    monitor_command.add_argument(
        "--checkpoint-dir",
        type=Path,
        default=Path(".twincoreos/checkpoints"),
    )
    monitor_command.add_argument(
        "--inject-fault-worker",
        metavar="WORKER_ID",
        help="terminate this supervised worker after its first telemetry sample, then attempt verified recovery",
    )
    serve_command = commands.add_parser(
        "serve", help="run the live monitoring API and dashboard backend"
    )
    serve_command.add_argument("--workers", type=int, default=2)
    serve_command.add_argument("--memory-mib", type=int, default=8)
    serve_command.add_argument("--sample-interval", type=float, default=1.0)
    serve_command.add_argument("--checkpoint-interval", type=float, default=5.0)
    serve_command.add_argument(
        "--checkpoint-budget-bytes", type=int, default=10 * 1024 * 1024
    )
    serve_command.add_argument(
        "--checkpoint-dir", type=Path, default=Path(".twincoreos/checkpoints")
    )
    serve_command.add_argument(
        "--embedded-stack-reserve-threshold-words",
        type=int,
        default=32,
        help="recommend embedded checkpoint at or below this FreeRTOS stack reserve",
    )
    serve_command.add_argument("--startup-timeout", type=float, default=10.0)
    serve_command.add_argument("--shutdown-timeout", type=float, default=5.0)
    serve_command.add_argument("--host", default="127.0.0.1")
    serve_command.add_argument("--port", type=int, default=8765)
    bridge_command = commands.add_parser(
        "embedded-bridge",
        help="run FreeRTOS firmware in QEMU and forward its UART telemetry",
    )
    bridge_command.add_argument(
        "--firmware",
        type=Path,
        default=Path(
            ".deps/freertos/FreeRTOS/Demo/RISC-V_RV32_QEMU_VIRT_GCC/"
            "build/gcc/output/RTOSDemo.elf"
        ),
    )
    bridge_command.add_argument(
        "--api-url",
        default="http://127.0.0.1:8765/api/embedded/telemetry",
    )
    bridge_command.add_argument("--qemu", default="qemu-system-riscv32")
    bridge_command.add_argument("--shutdown-timeout", type=float, default=5.0    )
    args = parser.parse_args()
    if args.command == "embedded-bridge":
        bridge_stop = threading.Event()

        def stop_bridge(_signum: int, _frame: object) -> None:
            bridge_stop.set()

        previous_sigint = signal.signal(signal.SIGINT, stop_bridge)
        previous_sigterm = signal.signal(signal.SIGTERM, stop_bridge)
        try:
            try:
                return run_bridge(
                    firmware=args.firmware,
                    api_url=args.api_url,
                    qemu=args.qemu,
                    shutdown_timeout=args.shutdown_timeout,
                    stop_requested=bridge_stop.is_set,
                )
            except EmbeddedBridgeError as error:
                print(f"Embedded bridge failed: {error}", file=sys.stderr)
                return 1
        finally:
            signal.signal(signal.SIGINT, previous_sigint)
            signal.signal(signal.SIGTERM, previous_sigterm)
    if args.command == "serve":
        if args.workers < 1:
            parser.error("--workers must be at least 1")
        if args.memory_mib < 1:
            parser.error("--memory-mib must be at least 1")
        if not math.isfinite(args.sample_interval) or args.sample_interval <= 0:
            parser.error("--sample-interval must be greater than 0")
        if (
            not math.isfinite(args.checkpoint_interval)
            or args.checkpoint_interval < args.sample_interval
        ):
            parser.error("--checkpoint-interval must be at least --sample-interval")
        if args.checkpoint_budget_bytes <= 0:
            parser.error("--checkpoint-budget-bytes must be greater than 0")
        if args.embedded_stack_reserve_threshold_words < 0:
            parser.error(
                "--embedded-stack-reserve-threshold-words must be non-negative"
            )
        if not math.isfinite(args.startup_timeout) or args.startup_timeout <= 0:
            parser.error("--startup-timeout must be greater than 0")
        if not math.isfinite(args.shutdown_timeout) or args.shutdown_timeout <= 0:
            parser.error("--shutdown-timeout must be greater than 0")
        if not 1 <= args.port <= 65535:
            parser.error("--port must be between 1 and 65535")
        runtime = MonitorRuntime(
            workers=args.workers,
            memory_mib=args.memory_mib,
            sample_interval=args.sample_interval,
            checkpoint_interval=args.checkpoint_interval,
            checkpoint_budget_bytes=args.checkpoint_budget_bytes,
            checkpoint_dir=args.checkpoint_dir,
            embedded_stack_reserve_threshold_words=(
                args.embedded_stack_reserve_threshold_words
            ),
            startup_timeout=args.startup_timeout,
            shutdown_timeout=args.shutdown_timeout,
        )
        api_stop = threading.Event()
        api_signal: int | None = None

        def stop_api(signum: int, _frame: object) -> None:
            nonlocal api_signal
            api_signal = signum
            api_stop.set()

        previous_sigint = signal.signal(signal.SIGINT, stop_api)
        previous_sigterm = signal.signal(signal.SIGTERM, stop_api)
        try:
            try:
                serve(
                    runtime,
                    host=args.host,
                    port=args.port,
                    stop_requested=api_stop.is_set,
                )
            except RuntimeUnavailableError as error:
                print(f"Could not start API: {error}", file=sys.stderr)
                return 1
            except OSError as error:
                print(f"Could not bind API server: {error}", file=sys.stderr)
                return 1
        finally:
            signal.signal(signal.SIGINT, previous_sigint)
            signal.signal(signal.SIGTERM, previous_sigterm)
        if api_stop.is_set():
            print("Interrupted; stopping API and workers.", file=sys.stderr)
            return 128 + (api_signal or signal.SIGINT)
        return 0

    if args.workers < 1:
        parser.error("--workers must be at least 1")
    if not math.isfinite(args.duration) or args.duration <= 0:
        parser.error("--duration must be greater than 0")
    if not math.isfinite(args.sample_interval) or args.sample_interval <= 0:
        parser.error("--sample-interval must be greater than 0")
    if args.sample_interval > args.duration:
        parser.error("--sample-interval must not exceed --duration")
    if args.memory_mib < 1:
        parser.error("--memory-mib must be at least 1")
    if not math.isfinite(args.startup_timeout) or args.startup_timeout <= 0:
        parser.error("--startup-timeout must be greater than 0")
    if not math.isfinite(args.shutdown_timeout) or args.shutdown_timeout <= 0:
        parser.error("--shutdown-timeout must be greater than 0")
    if not math.isfinite(args.checkpoint_interval) or args.checkpoint_interval <= 0:
        parser.error("--checkpoint-interval must be greater than 0")
    if args.checkpoint_budget_bytes <= 0:
        parser.error("--checkpoint-budget-bytes must be greater than 0")
    if args.checkpoint_interval < args.sample_interval:
        parser.error("--checkpoint-interval must be at least --sample-interval")
    valid_worker_ids = {f"worker-{index}" for index in range(1, args.workers + 1)}
    if args.inject_fault_worker is not None and args.inject_fault_worker not in valid_worker_ids:
        parser.error(
            "--inject-fault-worker must name a configured worker: "
            + ", ".join(sorted(valid_worker_ids))
        )

    stop_event = threading.Event()
    requested_signal: int | None = None

    def request_stop(signum: int, _frame: object) -> None:
        nonlocal requested_signal
        requested_signal = signum
        stop_event.set()

    previous_sigint_handler = signal.signal(signal.SIGINT, request_stop)
    previous_sigterm_handler = signal.signal(signal.SIGTERM, request_stop)
    try:
        try:
            monitor(
                workers=args.workers,
                duration=args.duration,
                sample_interval=args.sample_interval,
                memory_mib=args.memory_mib,
                startup_timeout=args.startup_timeout,
                shutdown_timeout=args.shutdown_timeout,
                checkpoint_interval=args.checkpoint_interval,
                checkpoint_budget_bytes=args.checkpoint_budget_bytes,
                checkpoint_dir=args.checkpoint_dir,
                fault_worker_id=args.inject_fault_worker,
                stop_requested=lambda: requested_signal,
                stop_event=stop_event,
            )
        except MonitoringInterrupted as error:
            if error.signum == signal.SIGINT:
                print("Interrupted; stopping workers.", file=sys.stderr)
            return 128 + error.signum
        except RecoveryError as error:
            print(f"Recovery failed: {error}", file=sys.stderr)
            return 1
    finally:
        signal.signal(signal.SIGINT, previous_sigint_handler)
        signal.signal(signal.SIGTERM, previous_sigterm_handler)
    return 0
