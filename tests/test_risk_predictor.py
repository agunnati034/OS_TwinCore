import unittest
from datetime import datetime, timedelta, timezone

import psutil

from twincoreos.digital_twin import DigitalTwinEngine, HealthState
from twincoreos.risk_predictor import (
    BaselineRiskPredictor,
    InvalidRiskInputError,
    RiskCategory,
    RiskThresholds,
)


def telemetry(
    *,
    worker_id: str = "worker-1",
    pid: int = 1234,
    elapsed: float = 1.0,
    captured_at: str = "2026-10-24T13:00:00+00:00",
    cpu: float | None = 20.0,
    rss: int | None = 8 * 1024 * 1024,
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
        "cpu_utilization_percent_one_core": cpu,
        "cpu_interval_seconds": 1.0,
        "cpu_utilization_scope": "one_logical_cpu_core",
        "memory_rss_bytes": rss,
        "process_status": status,
        "measurement_errors": errors or {},
    }


def twin_for(
    current: dict[str, object],
    previous: dict[str, object] | None = None,
    *,
    stale: bool = False,
    failed: bool = False,
):
    engine = DigitalTwinEngine()
    if previous is not None:
        engine.update(previous, observed_at_monotonic=1)
        state = engine.update(current, observed_at_monotonic=2)
    else:
        state = engine.update(current, observed_at_monotonic=1)
    state.stale = stale
    if failed:
        state.health_state = HealthState.FAILED
    return state


class BaselineRiskPredictorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.predictor = BaselineRiskPredictor()

    def test_low_risk_when_measurements_and_health_are_normal(self) -> None:
        assessment = self.predictor.assess(
            twin_for(telemetry()),
            assessed_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
        )

        self.assertEqual(assessment.risk_score, 0)
        self.assertEqual(assessment.risk_category, RiskCategory.LOW)
        self.assertIn("no elevated risk", assessment.explanation)
        self.assertTrue(assessment.is_valid(datetime(2026, 10, 24, 0, 0, 1, tzinfo=timezone.utc)))

    def test_medium_risk_for_elevated_cpu(self) -> None:
        twin = twin_for(telemetry(cpu=80))
        twin.health_state = HealthState.DEGRADED
        assessment = self.predictor.assess(
            twin,
            assessed_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
        )

        self.assertAlmostEqual(assessment.risk_score, 0.37)
        self.assertEqual(assessment.risk_category, RiskCategory.MEDIUM)

    def test_high_risk_when_cpu_and_resource_trends_increase(self) -> None:
        previous = telemetry(
            elapsed=1,
            captured_at="2026-10-24T13:00:00+00:00",
            cpu=70,
            rss=8 * 1024 * 1024,
        )
        current = telemetry(
            elapsed=2,
            captured_at="2026-10-24T13:00:01+00:00",
            cpu=95,
            rss=20 * 1024 * 1024,
        )
        assessment = self.predictor.assess(
            twin_for(current, previous),
            assessed_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
        )

        self.assertAlmostEqual(assessment.risk_score, 0.7)
        self.assertEqual(assessment.risk_category, RiskCategory.HIGH)
        self.assertEqual(assessment.observed_features["cpu_trend_percent_points"], 25)
        self.assertEqual(
            assessment.observed_features["memory_rss_trend_bytes"],
            12 * 1024 * 1024,
        )

    def test_cpu_and_memory_trends_are_reported_separately(self) -> None:
        previous = telemetry(cpu=40, rss=8 * 1024 * 1024)
        current = telemetry(
            elapsed=2,
            captured_at="2026-10-24T13:00:01+00:00",
            cpu=65,
            rss=12 * 1024 * 1024,
        )
        assessment = self.predictor.assess(twin_for(current, previous))

        self.assertEqual(
            assessment.observed_features["cpu_trend_percent_points"], 25
        )
        self.assertEqual(
            assessment.observed_features["memory_rss_trend_bytes"],
            4 * 1024 * 1024,
        )
        self.assertAlmostEqual(assessment.risk_score, 0.2)

    def test_failed_worker_has_maximum_risk(self) -> None:
        assessment = self.predictor.assess(
            twin_for(telemetry(status=psutil.STATUS_ZOMBIE), failed=True)
        )

        self.assertEqual(assessment.risk_score, 1)
        self.assertEqual(assessment.risk_category, RiskCategory.HIGH)
        self.assertTrue(assessment.observed_features["observed_worker_failure"])

    def test_stale_twin_adds_risk_without_claiming_failure(self) -> None:
        twin = twin_for(telemetry(), stale=True)
        twin.health_state = HealthState.DEGRADED
        assessment = self.predictor.assess(twin)

        self.assertAlmostEqual(assessment.risk_score, 0.45)
        self.assertEqual(assessment.risk_category, RiskCategory.MEDIUM)
        self.assertFalse(assessment.observed_features["observed_worker_failure"])
        self.assertTrue(assessment.observed_features["telemetry_stale"])

    def test_missing_measurements_are_explicit_and_increase_risk(self) -> None:
        twin = twin_for(
            telemetry(
                cpu=None,
                rss=None,
                errors={"cpu": "AccessDenied", "memory_rss_bytes": "AccessDenied"},
            )
        )
        assessment = self.predictor.assess(twin)

        self.assertEqual(assessment.observed_features["cpu_utilization_percent_one_core"], None)
        self.assertEqual(assessment.observed_features["memory_rss_bytes"], None)
        self.assertEqual(len(assessment.observed_features["missing_measurements"]), 2)
        self.assertAlmostEqual(assessment.risk_score, 0.45)

    def test_process_state_change_contributes_to_score(self) -> None:
        previous = telemetry(status=psutil.STATUS_RUNNING)
        current = telemetry(
            elapsed=2,
            captured_at="2026-10-24T13:00:01+00:00",
            status=psutil.STATUS_SLEEPING,
        )
        assessment = self.predictor.assess(twin_for(current, previous))

        self.assertTrue(assessment.observed_features["process_state_changed"])
        self.assertEqual(assessment.risk_score, 0.35)
        self.assertIn("process status changed", assessment.explanation)

    def test_multiple_workers_have_independent_assessment_history(self) -> None:
        first = self.predictor.assess(twin_for(telemetry()))
        second = self.predictor.assess(
            twin_for(telemetry(worker_id="worker-2", pid=1235, cpu=95))
        )
        next_first = self.predictor.assess(
            twin_for(
                telemetry(
                    elapsed=2,
                    captured_at="2026-10-24T13:00:01+00:00",
                    cpu=80,
                )
            )
        )

        self.assertIsNone(first.previous_category)
        self.assertIsNone(second.previous_category)
        self.assertEqual(next_first.previous_category, RiskCategory.LOW)
        self.assertNotEqual(first.pid, second.pid)

    def test_category_transition_is_reported_on_repeated_assessment(self) -> None:
        twin = twin_for(telemetry())
        first = self.predictor.assess(
            twin,
            assessed_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
        )
        twin.latest_observation = telemetry(cpu=95)
        twin.health_state = HealthState.HEALTHY
        second = self.predictor.assess(
            twin,
            assessed_at=datetime(2026, 10, 24, 0, 0, 1, tzinfo=timezone.utc),
        )

        self.assertEqual(first.risk_category, RiskCategory.LOW)
        self.assertEqual(second.risk_category, RiskCategory.MEDIUM)
        self.assertTrue(second.category_changed)
        self.assertEqual(second.previous_category, RiskCategory.LOW)

    def test_score_is_bounded_and_thresholds_are_configurable(self) -> None:
        thresholds = RiskThresholds(
            degraded_score=0.9,
            stale_score=0.9,
            missing_measurement_score=0.9,
        )
        predictor = BaselineRiskPredictor(thresholds=thresholds)
        twin = twin_for(
            telemetry(cpu=None, rss=None),
            stale=True,
        )
        twin.health_state = HealthState.DEGRADED
        assessment = predictor.assess(twin)

        self.assertEqual(assessment.risk_score, 1.0)
        self.assertGreaterEqual(assessment.risk_score, 0)
        self.assertLessEqual(assessment.risk_score, 1)

    def test_invalid_input_and_thresholds_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            RiskThresholds(cpu_high_percent=50, cpu_elevated_percent=75)
        with self.assertRaises(ValueError):
            BaselineRiskPredictor(assessment_validity_seconds=0)
        twin = twin_for(telemetry())
        twin.latest_observation["memory_rss_bytes"] = -1
        with self.assertRaises(InvalidRiskInputError):
            self.predictor.assess(twin)

    def test_assessment_validity_expires(self) -> None:
        assessment = self.predictor.assess(
            twin_for(telemetry()),
            assessed_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
        )

        self.assertFalse(
            assessment.is_valid(
                datetime(2026, 10, 24, tzinfo=timezone.utc) + timedelta(seconds=6)
            )
        )


if __name__ == "__main__":
    unittest.main()
