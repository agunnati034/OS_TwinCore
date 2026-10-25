import unittest

import psutil

from twincoreos.digital_twin import (
    DigitalTwinEngine,
    HealthState,
    InvalidObservationError,
)


def observation(
    worker_id: str = "worker-1",
    pid: int = 1234,
    captured_at: str = "2026-10-24T13:00:00+00:00",
    elapsed: float = 1.0,
    cpu_percent: float | None = 42.5,
    rss_bytes: int | None = 8_388_608,
    status: str | None = psutil.STATUS_RUNNING,
    errors: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "type": "telemetry",
        "task_id": worker_id,
        "pid": pid,
        "ppid": 1000,
        "process_created_at_utc": "2026-10-24T12:59:00+00:00",
        "captured_at_utc": captured_at,
        "monitor_elapsed_seconds": elapsed,
        "workload_elapsed_seconds": elapsed,
        "cpu_time_user_seconds_total": 1.0,
        "cpu_time_system_seconds_total": 0.1,
        "cpu_time_total_seconds": 1.1,
        "cpu_utilization_percent_one_core": cpu_percent,
        "cpu_interval_seconds": 1.0,
        "cpu_utilization_scope": "one_logical_cpu_core",
        "memory_rss_bytes": rss_bytes,
        "process_status": status,
        "measurement_errors": errors or {},
    }


class DigitalTwinTests(unittest.TestCase):
    def test_creates_twin_from_real_format_telemetry(self) -> None:
        engine = DigitalTwinEngine(stale_after_seconds=3)
        twin = engine.update(observation(), observed_at_monotonic=10)

        self.assertEqual(twin.worker_id, "worker-1")
        self.assertEqual(twin.pid, 1234)
        self.assertEqual(twin.health_state, HealthState.HEALTHY)
        snapshot = engine.snapshot(now_monotonic=11)
        self.assertEqual(snapshot["type"], "digital_twin_snapshot")
        self.assertEqual(snapshot["twins"][0]["memory_rss_bytes"], 8_388_608)
        self.assertEqual(
            snapshot["twins"][0]["cpu_utilization_percent_one_core"], 42.5
        )

    def test_successive_observations_preserve_previous_state(self) -> None:
        engine = DigitalTwinEngine()
        engine.update(observation(), observed_at_monotonic=10)
        updated = engine.update(
            observation(
                captured_at="2026-10-24T13:00:01+00:00",
                elapsed=2,
                cpu_percent=71.0,
                rss_bytes=9_000_000,
            ),
            observed_at_monotonic=11,
        )

        self.assertEqual(updated.health_state, HealthState.HEALTHY)
        self.assertEqual(
            updated.previous_observation["cpu_utilization_percent_one_core"], 42.5
        )
        self.assertEqual(
            updated.latest_observation["cpu_utilization_percent_one_core"], 71.0
        )

    def test_unavailable_field_degrades_but_does_not_fail_worker(self) -> None:
        engine = DigitalTwinEngine()
        twin = engine.update(
            observation(
                cpu_percent=None,
                errors={"cpu": "AccessDenied"},
            ),
            observed_at_monotonic=1,
        )

        self.assertEqual(twin.health_state, HealthState.DEGRADED)
        self.assertIsNone(
            engine.snapshot(now_monotonic=1)["twins"][0][
                "cpu_utilization_percent_one_core"
            ]
        )

    def test_degraded_worker_recovers_when_measurements_return(self) -> None:
        engine = DigitalTwinEngine()
        engine.update(
            observation(rss_bytes=None, errors={"memory_rss_bytes": "AccessDenied"}),
            observed_at_monotonic=1,
        )
        recovered = engine.update(
            observation(captured_at="2026-10-24T13:00:01+00:00"),
            observed_at_monotonic=2,
        )

        self.assertEqual(recovered.previous_health_state, HealthState.DEGRADED)
        self.assertEqual(recovered.health_state, HealthState.HEALTHY)

    def test_stale_observation_degrades_but_does_not_fail(self) -> None:
        engine = DigitalTwinEngine(stale_after_seconds=2)
        twin = engine.update(observation(), observed_at_monotonic=10)

        snapshot = engine.snapshot(now_monotonic=12.1)

        self.assertEqual(twin.health_state, HealthState.DEGRADED)
        self.assertTrue(snapshot["twins"][0]["stale"])
        self.assertEqual(snapshot["twins"][0]["health_state"], "DEGRADED")

    def test_observed_termination_marks_twin_failed(self) -> None:
        engine = DigitalTwinEngine()
        engine.update(observation(), observed_at_monotonic=1)
        failed = engine.mark_failed(
            "worker-1",
            1234,
            "worker process exited with status 1",
            observed_at_monotonic=2,
        )

        self.assertEqual(failed.health_state, HealthState.FAILED)
        self.assertEqual(failed.failure_reason, "worker process exited with status 1")
        engine.refresh(now_monotonic=100)
        self.assertEqual(failed.health_state, HealthState.FAILED)

    def test_terminal_process_status_marks_worker_failed(self) -> None:
        engine = DigitalTwinEngine()
        twin = engine.update(
            observation(status=psutil.STATUS_ZOMBIE),
            observed_at_monotonic=1,
        )

        self.assertEqual(twin.health_state, HealthState.FAILED)

    def test_multiple_workers_are_isolated(self) -> None:
        engine = DigitalTwinEngine()
        engine.update(observation(), observed_at_monotonic=1)
        engine.update(
            observation(
                worker_id="worker-2",
                pid=1235,
                captured_at="2026-10-24T13:00:00+00:00",
                cpu_percent=None,
                errors={"cpu": "AccessDenied"},
            ),
            observed_at_monotonic=1,
        )

        snapshot = engine.snapshot(now_monotonic=2)

        self.assertEqual(
            [twin["health_state"] for twin in snapshot["twins"]],
            ["HEALTHY", "DEGRADED"],
        )
        self.assertEqual(snapshot["twins"][0]["pid"], 1234)
        self.assertEqual(snapshot["twins"][1]["pid"], 1235)

    def test_out_of_order_observation_is_rejected_without_mutating_twin(self) -> None:
        engine = DigitalTwinEngine()
        engine.update(observation(), observed_at_monotonic=1)

        with self.assertRaises(InvalidObservationError):
            engine.update(
                observation(
                    captured_at="2026-10-24T12:59:59+00:00",
                    elapsed=2,
                ),
                observed_at_monotonic=2,
            )

        self.assertEqual(
            engine.get("worker-1").latest_observation["captured_at_utc"],
            "2026-10-24T13:00:00+00:00",
        )

    def test_pid_change_for_existing_worker_is_rejected(self) -> None:
        engine = DigitalTwinEngine()
        engine.update(observation(), observed_at_monotonic=1)

        with self.assertRaisesRegex(InvalidObservationError, "PID changed"):
            engine.update(
                observation(pid=4321, captured_at="2026-10-24T13:00:01+00:00"),
                observed_at_monotonic=2,
            )

    def test_invalid_measurements_are_rejected(self) -> None:
        engine = DigitalTwinEngine()
        with self.assertRaisesRegex(InvalidObservationError, "cpu utilization"):
            engine.update(
                observation(cpu_percent=float("nan")),
                observed_at_monotonic=1,
            )
        with self.assertRaisesRegex(InvalidObservationError, "memory_rss_bytes"):
            engine.update(
                observation(rss_bytes=-1),
                observed_at_monotonic=1,
            )


if __name__ == "__main__":
    unittest.main()
