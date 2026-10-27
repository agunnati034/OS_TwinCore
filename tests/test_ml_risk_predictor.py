import json
from datetime import datetime, timezone
import tempfile
from pathlib import Path
import unittest

import psutil
import joblib

from twincoreos.digital_twin import DigitalTwinEngine, HealthState
from twincoreos.ml_risk_predictor import (
    FEATURE_NAMES,
    FallbackRiskPredictor,
    IsolationForestRiskPredictor,
    create_risk_predictor,
    extract_features,
    load_isolation_forest,
    read_jsonl_telemetry,
    telemetry_feature_rows,
    train_isolation_forest,
)
from twincoreos.risk_predictor import InvalidRiskInputError, RiskCategory


def telemetry(
    *,
    captured_seconds: int = 0,
    cpu: float | None = 20.0,
    rss: int | None = 8 * 1024 * 1024,
    status: str = psutil.STATUS_RUNNING,
    errors: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "type": "telemetry",
        "task_id": "worker-1",
        "pid": 1234,
        "ppid": 1000,
        "process_created_at_utc": "2026-10-24T12:59:00+00:00",
        "captured_at_utc": (
            f"2026-10-24T13:00:{captured_seconds:02d}+00:00"
        ),
        "monitor_elapsed_seconds": captured_seconds + 1.0,
        "workload_elapsed_seconds": captured_seconds + 1.0,
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


def twin_from_samples(*samples: dict[str, object]):
    engine = DigitalTwinEngine()
    twin = None
    for index, sample in enumerate(samples, start=1):
        twin = engine.update(sample, observed_at_monotonic=float(index))
    return twin


class FixedPipeline:
    def __init__(self, decision: float) -> None:
        self.decision = decision
        self.calls = 0

    def decision_function(self, values: list[list[float]]) -> list[float]:
        self.calls += 1
        return [self.decision]


class FailingPipeline:
    def decision_function(self, values: list[list[float]]) -> list[float]:
        raise RuntimeError("injected inference failure")


class MlRiskPredictorTests(unittest.TestCase):
    def test_features_have_fixed_order_and_include_health_freshness_and_status(self) -> None:
        previous = telemetry()
        current = telemetry(
            captured_seconds=1,
            cpu=None,
            errors={"cpu": "AccessDenied"},
            status=psutil.STATUS_SLEEPING,
        )
        twin = twin_from_samples(previous, current)
        twin.stale = True

        vector, summary = extract_features(twin)

        self.assertEqual(len(vector), len(FEATURE_NAMES))
        self.assertEqual(summary["health_state"], HealthState.DEGRADED.value)
        self.assertTrue(summary["telemetry_stale"])
        self.assertTrue(summary["cpu_missing"])
        self.assertTrue(summary["process_state_changed"])
        self.assertTrue(summary["cpu_trend_missing"])
        self.assertIsNone(summary["cpu_utilization_percent_one_core"])
        self.assertEqual(summary["measurement_errors"], {"cpu": "AccessDenied"})
        self.assertEqual(
            vector[FEATURE_NAMES.index("telemetry_gap_seconds")],
            1.0,
        )

    def test_invalid_measurements_are_rejected(self) -> None:
        twin = twin_from_samples(telemetry())
        twin.latest_observation["memory_rss_bytes"] = -1

        with self.assertRaises(InvalidRiskInputError):
            extract_features(twin)

        twin.latest_observation["memory_rss_bytes"] = 1.5
        with self.assertRaises(InvalidRiskInputError):
            extract_features(twin)

        twin.latest_observation["memory_rss_bytes"] = 10**1000
        with self.assertRaises(InvalidRiskInputError):
            extract_features(twin)

    def test_inference_exposes_anomaly_score_and_explicit_category_mapping(self) -> None:
        twin = twin_from_samples(
            telemetry(captured_seconds=0),
            telemetry(captured_seconds=1),
        )
        anomalous = IsolationForestRiskPredictor(
            FixedPipeline(-0.2),
            normal_risk_score=0.0,
            anomaly_risk_score=0.6,
        ).assess(twin)

        self.assertEqual(anomalous.risk_category, RiskCategory.HIGH)
        self.assertEqual(anomalous.risk_score, 0.6)
        self.assertEqual(
            anomalous.observed_features["anomaly_status"], "anomalous"
        )
        self.assertAlmostEqual(
            anomalous.observed_features["anomaly_score"], 0.2
        )
        self.assertIn("not a failure probability", anomalous.explanation)

        inlier = IsolationForestRiskPredictor(
            FixedPipeline(0.2),
            normal_risk_score=0.3,
            anomaly_risk_score=0.6,
        ).assess(twin)
        self.assertEqual(inlier.risk_category, RiskCategory.MEDIUM)
        self.assertEqual(inlier.observed_features["anomaly_status"], "inlier")
        with self.assertRaisesRegex(ValueError, "must be >="):
            IsolationForestRiskPredictor(
                FixedPipeline(0.0),
                normal_risk_score=0.7,
                anomaly_risk_score=0.6,
            )

    def test_new_worker_risk_warms_up_before_ml_score_is_assigned(self) -> None:
        engine = DigitalTwinEngine()
        twin = engine.update(telemetry(captured_seconds=0), observed_at_monotonic=1)
        model = FixedPipeline(-0.2)
        predictor = FallbackRiskPredictor(
            IsolationForestRiskPredictor(model, anomaly_risk_score=0.6),
            assessment_validity_seconds=5,
        )

        warming = predictor.assess(twin)
        self.assertEqual(warming.risk_category, RiskCategory.WARMING_UP)
        self.assertIsNone(warming.risk_score)
        self.assertEqual(model.calls, 0)
        self.assertIn("two telemetry samples", warming.explanation)

        engine.update(
            telemetry(captured_seconds=1),
            observed_at_monotonic=2,
        )
        assessed = predictor.assess(twin)
        self.assertEqual(assessed.risk_category, RiskCategory.HIGH)
        self.assertEqual(assessed.risk_score, 0.6)
        self.assertEqual(model.calls, 1)
        self.assertEqual(
            assessed.observed_features["risk_score_semantics"],
            "configured category-compatibility index; not a failure probability",
        )

    def test_failed_worker_uses_deterministic_baseline_without_ml_inference(self) -> None:
        engine = DigitalTwinEngine()
        twin = engine.update(telemetry(), observed_at_monotonic=1)
        engine.mark_failed("worker-1", 1234, "worker exited", observed_at_monotonic=2)
        pipeline = FixedPipeline(-1.0)
        predictor = FallbackRiskPredictor(
            IsolationForestRiskPredictor(pipeline),
            assessment_validity_seconds=5,
        )

        assessment = predictor.assess(twin)

        self.assertEqual(assessment.risk_score, 1.0)
        self.assertEqual(assessment.risk_category, RiskCategory.HIGH)
        self.assertEqual(pipeline.calls, 0)
        self.assertEqual(
            assessment.observed_features["model_type"],
            "baseline_failure_override",
        )

    def test_inference_failure_is_logged_and_falls_back_to_baseline(self) -> None:
        twin = twin_from_samples(
            telemetry(captured_seconds=0, cpu=20, rss=8 * 1024 * 1024),
            telemetry(captured_seconds=1, cpu=95, rss=20 * 1024 * 1024),
        )
        predictor = FallbackRiskPredictor(
            IsolationForestRiskPredictor(FailingPipeline()),
            assessment_validity_seconds=5,
        )

        with self.assertLogs("twincoreos.ml_risk_predictor", level="ERROR"):
            assessment = predictor.assess(twin)

        self.assertEqual(assessment.risk_category, RiskCategory.HIGH)
        self.assertEqual(
            assessment.observed_features["model_type"], "baseline_fallback"
        )
        self.assertIn("injected inference failure", assessment.explanation)

    def test_missing_or_invalid_model_artifact_uses_explicit_baseline_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing_path = Path(directory) / "missing.joblib"
            missing_predictor = create_risk_predictor(
                missing_path,
                assessment_validity_seconds=5,
            )
            assessment = missing_predictor.assess(
                twin_from_samples(telemetry(captured_seconds=0), telemetry(captured_seconds=1))
            )
            self.assertEqual(
                assessment.observed_features["model_type"], "baseline_fallback"
            )
            self.assertIn(
                "FileNotFoundError",
                assessment.observed_features["fallback_reason"],
            )
            invalid_path = Path(directory) / "invalid.joblib"
            joblib.dump({"artifact_version": -1}, invalid_path)
            invalid_predictor = create_risk_predictor(
                invalid_path,
                assessment_validity_seconds=5,
            )
            invalid_assessment = invalid_predictor.assess(
                twin_from_samples(
                    telemetry(captured_seconds=0), telemetry(captured_seconds=1)
                )
            )
            self.assertIn(
                "unsupported model artifact version",
                invalid_assessment.observed_features["fallback_reason"],
            )

    def test_training_and_loading_persist_a_real_sklearn_pipeline(self) -> None:
        records = [
            telemetry(
                captured_seconds=second,
                cpu=None if second % 5 == 0 else 20 + second,
                rss=None if second % 5 == 0 else 8 * 1024 * 1024 + second * 1024,
                errors=(
                    {"cpu": "AccessDenied", "memory_rss_bytes": "AccessDenied"}
                    if second % 5 == 0
                    else {}
                ),
            )
            for second in range(25)
        ]
        rows = telemetry_feature_rows(records)
        self.assertEqual(len(rows), 25)
        self.assertTrue(all(len(row) == len(FEATURE_NAMES) for row in rows))

        with tempfile.TemporaryDirectory() as directory:
            artifact_path = Path(directory) / "model.joblib"
            report = train_isolation_forest(rows, artifact_path)
            predictor = load_isolation_forest(artifact_path)
            assessment = predictor.assess(
                twin_from_samples(
                    telemetry(captured_seconds=0), telemetry(captured_seconds=1)
                ),
                assessed_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
            )

            self.assertEqual(report["training_samples"], 25)
            self.assertTrue(artifact_path.is_file())
            self.assertEqual(
                assessment.observed_features["model_type"], "isolation_forest"
            )
            self.assertIn(
                assessment.observed_features["anomaly_status"],
                {"inlier", "anomalous"},
            )

    def test_training_refuses_too_few_samples_and_wrong_feature_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "at least 20"):
                train_isolation_forest(
                    [[0.0] * len(FEATURE_NAMES)] * 19,
                    Path(directory) / "model.joblib",
                )
            with self.assertRaisesRegex(ValueError, "fixed feature schema"):
                train_isolation_forest(
                    [[0.0] * (len(FEATURE_NAMES) - 1)] * 20,
                    Path(directory) / "model.joblib",
                )

    def test_jsonl_reader_ignores_non_host_events_and_rejects_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "telemetry.jsonl"
            source.write_text(
                json.dumps({"type": "digital_twin_snapshot"})
                + "\n"
                + json.dumps(telemetry())
                + "\n",
                encoding="utf-8",
            )
            self.assertEqual(len(read_jsonl_telemetry(source)), 1)

            source.write_text("{bad json\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid JSON"):
                read_jsonl_telemetry(source)

if __name__ == "__main__":
    unittest.main()
