import json
import subprocess
import sys
import time
import unittest
from unittest import mock

import psutil

from twincoreos.monitor import (
    TelemetryCollectionError,
    WorkerPool,
    WorkerStartupError,
    cpu_utilization_percent,
)


def ready_worker_source() -> str:
    return (
        "import json,time; "
        "print(json.dumps({'event':'ready',"
        "'workload_started_monotonic':time.monotonic()}),flush=True); "
        "import signal; signal.pause()"
    )


class CpuUtilizationTests(unittest.TestCase):
    def test_calculates_cpu_utilization_over_elapsed_interval(self) -> None:
        self.assertAlmostEqual(cpu_utilization_percent(2.0, 2.2, 0.5), 40.0)

    def test_rejects_invalid_elapsed_interval(self) -> None:
        with self.assertRaises(ValueError):
            cpu_utilization_percent(0.0, 1.0, 0.0)

    def test_rejects_decreasing_cumulative_cpu_time(self) -> None:
        with self.assertRaises(ValueError):
            cpu_utilization_percent(2.0, 1.0, 1.0)


class WorkerPoolUnitTests(unittest.TestCase):
    def test_access_denied_measurement_is_null_and_reported(self) -> None:
        pid = None
        with WorkerPool(
            workers=1,
            memory_mib=1,
            worker_command=[sys.executable, "-c", ready_worker_source()],
        ) as pool:
            pid = pool._processes[0].child.pid
            with mock.patch.object(
                psutil.Process,
                "memory_info",
                side_effect=psutil.AccessDenied(pid=pool._processes[0].child.pid),
            ):
                sample = pool.sample(time.monotonic())[0]

            self.assertIsNone(sample["memory_rss_bytes"])
            self.assertIn("memory_rss_bytes", sample["measurement_errors"])
            self.assertIsNotNone(sample["cpu_utilization_percent_one_core"])
        self.assertFalse(psutil.pid_exists(pid))

    def test_access_denied_cpu_measurement_is_not_replaced(self) -> None:
        with WorkerPool(
            workers=1,
            memory_mib=1,
            worker_command=[sys.executable, "-c", ready_worker_source()],
        ) as pool:
            with mock.patch.object(
                psutil.Process,
                "cpu_times",
                side_effect=psutil.AccessDenied(pid=pool._processes[0].child.pid),
            ):
                pid = pool._processes[0].child.pid
                sample = pool.sample(time.monotonic())[0]

            self.assertIsNone(sample["cpu_time_user_seconds_total"])
            self.assertIsNone(sample["cpu_time_system_seconds_total"])
            self.assertIsNone(sample["cpu_time_total_seconds"])
            self.assertIsNone(sample["cpu_utilization_percent_one_core"])
            self.assertIsNone(sample["cpu_interval_seconds"])
            self.assertIn("cpu", sample["measurement_errors"])
        self.assertFalse(psutil.pid_exists(pid))

    def test_fatal_collection_error_raises_and_context_cleans_worker(self) -> None:
        pool = WorkerPool(
            workers=1,
            memory_mib=1,
            worker_command=[sys.executable, "-c", ready_worker_source()],
        )
        with pool:
            pid = pool._processes[0].child.pid
            with mock.patch.object(
                psutil.Process,
                "cpu_times",
                side_effect=psutil.Error("injected collection failure"),
            ):
                with self.assertRaisesRegex(
                    TelemetryCollectionError, "Fatal CPU collection error"
                ):
                    pool.sample(time.monotonic())

        self.assertFalse(psutil.pid_exists(pid))

    def test_worker_count_is_preserved(self) -> None:
        with WorkerPool(
            workers=3,
            memory_mib=1,
            worker_command=[sys.executable, "-c", ready_worker_source()],
        ) as pool:
            self.assertEqual(len(pool._processes), 3)
            self.assertEqual(len(pool.sample(time.monotonic())), 3)

    def test_stubborn_worker_is_killed_after_shutdown_timeout(self) -> None:
        source = (
            "import json,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            "print(json.dumps({'event':'ready',"
            "'workload_started_monotonic':time.monotonic()}),flush=True); "
            "signal.pause()"
        )
        with WorkerPool(
            workers=1,
            memory_mib=1,
            shutdown_timeout=0.1,
            worker_command=[sys.executable, "-c", source],
        ) as pool:
            pid = pool._processes[0].child.pid

        self.assertFalse(psutil.pid_exists(pid))


if __name__ == "__main__":
    unittest.main()
