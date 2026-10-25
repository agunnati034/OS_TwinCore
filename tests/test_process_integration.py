import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import psutil

from twincoreos.monitor import WorkerExitedError, WorkerPool, WorkerStartupError


class WorkerProcessIntegrationTests(unittest.TestCase):
    def test_real_worker_is_ready_before_first_sample(self) -> None:
        with WorkerPool(workers=1, memory_mib=4) as pool:
            worker = pool._processes[0]
            self.assertGreater(worker.workload_started_monotonic, 0)
            self.assertGreaterEqual(worker.process.memory_info().rss, 4 * 1024 * 1024)

            time.sleep(0.2)
            sample = pool.sample(time.monotonic())[0]

            self.assertGreater(sample["cpu_interval_seconds"], 0)
            self.assertGreater(sample["cpu_time_total_seconds"], 0)
            self.assertGreaterEqual(sample["workload_elapsed_seconds"], 0)
            self.assertEqual(sample["cpu_utilization_scope"], "one_logical_cpu_core")

    def test_cli_first_sample_follows_readiness_and_cpu_interval(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "twincoreos",
                "monitor",
                "--workers",
                "1",
                "--duration",
                "0.3",
                "--sample-interval",
                "0.2",
                "--memory-mib",
                "4",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        sample = json.loads(result.stdout.splitlines()[0])
        self.assertGreaterEqual(sample["memory_rss_bytes"], 4 * 1024 * 1024)
        self.assertGreaterEqual(sample["cpu_interval_seconds"], 0.19)
        self.assertGreaterEqual(sample["workload_elapsed_seconds"], 0.19)

    def test_cli_emits_per_worker_digital_twin_snapshots(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "twincoreos",
                "monitor",
                "--workers",
                "2",
                "--duration",
                "0.3",
                "--sample-interval",
                "0.2",
                "--memory-mib",
                "2",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        records = [json.loads(line) for line in result.stdout.splitlines()]
        telemetry = [record for record in records if record["type"] == "telemetry"]
        snapshots = [
            record
            for record in records
            if record["type"] == "digital_twin_snapshot"
        ]
        assessments = [
            record for record in records if record["type"] == "risk_assessment"
        ]
        self.assertEqual(len(telemetry), 2)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(len(assessments), 2)
        twins = snapshots[0]["twins"]
        self.assertEqual(len(twins), 2)
        self.assertEqual({twin["pid"] for twin in twins}, {t["pid"] for t in telemetry})
        self.assertTrue(all(twin["health_state"] == "HEALTHY" for twin in twins))
        self.assertEqual({assessment["pid"] for assessment in assessments}, {t["pid"] for t in telemetry})

    def test_cli_emits_checkpoint_events_and_storage_usage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "twincoreos",
                    "monitor",
                    "--workers",
                    "1",
                    "--duration",
                    "0.5",
                    "--sample-interval",
                    "0.2",
                    "--memory-mib",
                    "1",
                    "--checkpoint-interval",
                    "0.2",
                    "--checkpoint-dir",
                    directory,
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            records = [
                json.loads(line) for line in result.stdout.splitlines()
            ]
            checkpoint_events = [
                record
                for record in records
                if record["type"] == "checkpoint_event"
            ]
            statuses = [
                record
                for record in records
                if record["type"] == "checkpoint_status"
            ]

        self.assertGreaterEqual(len(checkpoint_events), 1)
        self.assertTrue(
            all(event["event"] == "created" for event in checkpoint_events)
        )
        self.assertGreater(checkpoint_events[0]["size_bytes"], 0)
        self.assertGreaterEqual(statuses[0]["storage_usage_bytes"], 0)
        self.assertLessEqual(
            statuses[0]["storage_usage_bytes"],
            statuses[0]["storage_budget_bytes"],
        )

    def test_cli_fault_injection_recovers_one_worker_with_fresh_pid(self) -> None:
        with tempfile.TemporaryDirectory() as checkpoint_dir:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "twincoreos",
                    "monitor",
                    "--workers",
                    "2",
                    "--duration",
                    "0.5",
                    "--sample-interval",
                    "0.2",
                    "--memory-mib",
                    "1",
                    "--inject-fault-worker",
                    "worker-1",
                    "--checkpoint-dir",
                    checkpoint_dir,
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
        records = [json.loads(line) for line in result.stdout.splitlines()]
        events = [item for item in records if item["type"] == "recovery_event"]
        completed = next(
            item for item in events if item["event"] == "recovery_completed"
        )
        self.assertNotEqual(completed["failed_pid"], completed["replacement_pid"])
        self.assertEqual(completed["restored_state"]["counter"], 1)
        self.assertEqual(completed["continued_state"]["counter"], 2)
        self.assertTrue(
            any(
                item["type"] == "telemetry"
                and item["task_id"] == completed["replacement_worker_id"]
                and item["pid"] == completed["replacement_pid"]
                for item in records
            )
        )

    def test_worker_early_exit_during_startup_is_reported(self) -> None:
        with self.assertRaisesRegex(
            WorkerStartupError,
            "exited during startup|closed its readiness pipe",
        ):
            with WorkerPool(
                workers=1,
                memory_mib=1,
                startup_timeout=2,
                worker_command=[sys.executable, "-c", "pass"],
            ):
                self.fail("unexpected readiness")

    def test_worker_initialization_error_is_reported(self) -> None:
        source = (
            "import json; "
            "print(json.dumps({'event':'error','error_type':'MemoryError',"
            "'message':'injected allocation failure'}),flush=True); "
            "raise SystemExit(1)"
        )
        with self.assertRaisesRegex(
            WorkerStartupError, "MemoryError: injected allocation failure"
        ):
            with WorkerPool(
                workers=1,
                memory_mib=1,
                worker_command=[sys.executable, "-c", source],
            ):
                self.fail("unexpected readiness")

    def test_startup_timeout_is_reported_and_worker_is_cleaned(self) -> None:
        pool = WorkerPool(
            workers=1,
            memory_mib=1,
            startup_timeout=0.2,
            shutdown_timeout=0.2,
            worker_command=[sys.executable, "-c", "import time; time.sleep(30)"],
        )
        with self.assertRaisesRegex(WorkerStartupError, "Timed out"):
            with pool:
                self.fail("unexpected readiness")
        self.assertTrue(all(worker.child.poll() is not None for worker in pool._processes))

    def test_worker_exit_during_monitoring_is_reported(self) -> None:
        source = (
            "import json,time; "
            "print(json.dumps({'event':'ready',"
            "'workload_started_monotonic':time.monotonic()}),flush=True)"
        )
        with WorkerPool(
            workers=1,
            memory_mib=1,
            worker_command=[sys.executable, "-c", source],
        ) as pool:
            pid = pool._processes[0].child.pid
            pool._processes[0].child.wait(timeout=2)
            with self.assertRaisesRegex(WorkerExitedError, "exited unexpectedly"):
                pool.sample(time.monotonic())
        self.assertFalse(psutil.pid_exists(pid))

    def test_invalid_cli_arguments_are_rejected(self) -> None:
        for arguments in (
            ["--workers", "0"],
            ["--duration", "0"],
            ["--sample-interval", "2", "--duration", "1"],
            ["--memory-mib", "0"],
            ["--startup-timeout", "nan"],
            ["--shutdown-timeout", "-1"],
            ["--workers", "2", "--inject-fault-worker", "worker-3"],
        ):
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "twincoreos",
                    "monitor",
                    *arguments,
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2, arguments)
            self.assertIn("error:", result.stderr, arguments)

    def _run_with_signal(self, requested_signal: signal.Signals) -> None:
        command = [
            sys.executable,
            "-m",
            "twincoreos",
            "monitor",
            "--workers",
            "2",
            "--duration",
            "30",
            "--sample-interval",
            "1",
            "--memory-mib",
            "1",
        ]
        monitor = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        pids: set[int] = set()
        try:
            self.assertIsNotNone(monitor.stdout)
            for _ in range(2):
                line = monitor.stdout.readline()
                self.assertTrue(line)
                pids.add(json.loads(line)["pid"])
            self.assertEqual(len(pids), 2)

            os.killpg(monitor.pid, requested_signal)
            return_code = monitor.wait(timeout=8)
            stderr = monitor.stderr.read() if monitor.stderr else ""
            self.assertEqual(return_code, 130 if requested_signal == signal.SIGINT else 143)
            if requested_signal == signal.SIGINT:
                self.assertIn("Interrupted; stopping workers.", stderr)
                self.assertNotIn("Traceback", stderr)
            else:
                self.assertEqual(stderr, "")

            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and any(
                psutil.pid_exists(pid) for pid in pids
            ):
                time.sleep(0.02)
            self.assertFalse([pid for pid in pids if psutil.pid_exists(pid)])
        finally:
            if monitor.poll() is None:
                os.killpg(monitor.pid, signal.SIGKILL)
                monitor.wait()
            if monitor.stdout is not None:
                monitor.stdout.close()
            if monitor.stderr is not None:
                monitor.stderr.close()
            for pid in pids:
                try:
                    psutil.Process(pid).kill()
                except psutil.NoSuchProcess:
                    pass

    def test_sigint_cleans_up_workers_without_traceback(self) -> None:
        self._run_with_signal(signal.SIGINT)

    def test_sigterm_cleans_up_workers(self) -> None:
        self._run_with_signal(signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
