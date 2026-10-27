from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
import uuid

import joblib
from sklearn.ensemble import IsolationForest

from twincoreos.embedded_ml import (
    FEATURE_NAMES,
    EmbeddedFeatureHistory,
    EmbeddedMLDataError,
    EmbeddedMLShadowMode,
    EmbeddedModelError,
    curate_baseline_session,
    evaluate_embedded_policy,
    generate_simulated_scenarios,
    load_embedded_model,
    make_live_dataset_record,
    train_embedded_model,
    validate_dataset_record,
    infer_embedded_anomaly,
)
from twincoreos.runtime import MonitorRuntime


_BASE_TIME = datetime(2025, 1, 1, tzinfo=timezone.utc)


def _telemetry(
    task_id: str,
    index: int,
    *,
    stack_words: int = 160,
    run_id: str | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "message_type": "task_telemetry",
        "target": "freertos-qemu-rv32",
        "task_id": task_id,
        "task_state": "running",
        "tick_count": 100 + index * 10,
        "execution_count": 2 + index,
        "stack_high_water_mark_words": stack_words,
    }
    if run_id is not None:
        value["run_id"] = run_id
    return value


def _live_records(
    session_id: str,
    *,
    count_per_task: int = 12,
) -> list[dict[str, object]]:
    history = EmbeddedFeatureHistory()
    records: list[dict[str, object]] = []
    for task_id in ("sensor-sampler", "control-loop"):
        for index in range(count_per_task):
            telemetry = _telemetry(task_id, index)
            observed_at = (_BASE_TIME + timedelta(seconds=index)).isoformat()
            features = history.extract(
                telemetry,
                bridge_session_id=session_id,
                observed_at_utc=observed_at,
                telemetry_age_seconds=0.01,
                telemetry_fresh=True,
            )
            records.append(
                make_live_dataset_record(
                    telemetry,
                    bridge_session_id=session_id,
                    observed_at_utc=observed_at,
                    telemetry_age_seconds=0.01,
                    telemetry_fresh=True,
                    features=features,
                )
            )
    return records


class EmbeddedMLTests(unittest.TestCase):
    def test_raw_anomaly_score_sign_matches_isolation_forest_prediction(self) -> None:
        rows = [
            [index / 1000, *([0.0] * (len(FEATURE_NAMES) - 1))]
            for index in range(50)
        ]
        pipeline = IsolationForest(contamination=0.05, random_state=42).fit(rows)
        artifact = {
            "pipeline": pipeline,
            "model_type": "isolation_forest",
            "model_version": "test-iforest",
        }

        for first_feature, expected_prediction in (
            (0.025, "inlier"),
            (1000.0, "anomaly"),
        ):
            features = {name: 0.0 for name in FEATURE_NAMES}
            features[FEATURE_NAMES[0]] = first_feature
            result = infer_embedded_anomaly(
                artifact,
                task_id="sensor-sampler",
                observed_at_utc=_BASE_TIME.isoformat(),
                telemetry_fresh=True,
                features=features,
            )

            decision_value = result["raw_decision_function"]
            self.assertEqual(result["prediction"], expected_prediction)
            self.assertAlmostEqual(result["raw_anomaly_score"], -decision_value)
            if expected_prediction == "inlier":
                self.assertGreater(decision_value, 0)
                self.assertLess(result["raw_anomaly_score"], 0)
            else:
                self.assertLess(decision_value, 0)
                self.assertGreater(result["raw_anomaly_score"], 0)

    def test_feature_history_tracks_deltas_and_resets_on_run_or_session(self) -> None:
        history = EmbeddedFeatureHistory(stale_after_seconds=3.0)
        first = history.extract(
            _telemetry("sensor-sampler", 0),
            bridge_session_id="session-a",
            observed_at_utc=_BASE_TIME.isoformat(),
            telemetry_age_seconds=0.02,
            telemetry_fresh=True,
        )
        self.assertEqual(tuple(first), FEATURE_NAMES)
        self.assertEqual(first["has_temporal_context"], 0.0)

        second = history.extract(
            _telemetry("sensor-sampler", 1, stack_words=155),
            bridge_session_id="session-a",
            observed_at_utc=(_BASE_TIME + timedelta(seconds=1)).isoformat(),
            telemetry_age_seconds=0.03,
            telemetry_fresh=True,
        )
        self.assertEqual(second["stack_reserve_delta_words"], -5.0)
        self.assertEqual(second["execution_delta"], 1.0)
        self.assertEqual(second["tick_delta"], 10.0)
        self.assertEqual(second["has_temporal_context"], 1.0)

        new_run_id = "0d179e32-3fbc-4ee1-830b-b69256131d89"
        restarted = history.extract(
            _telemetry("sensor-sampler", 0, run_id=new_run_id),
            bridge_session_id="session-a",
            observed_at_utc=(_BASE_TIME + timedelta(seconds=2)).isoformat(),
            telemetry_age_seconds=0.01,
            telemetry_fresh=True,
        )
        self.assertEqual(restarted["has_temporal_context"], 0.0)
        history.reset(bridge_session_id="session-a", task_id="sensor-sampler")
        after_reset = history.extract(
            _telemetry("sensor-sampler", 1),
            bridge_session_id="session-a",
            observed_at_utc=(_BASE_TIME + timedelta(seconds=3)).isoformat(),
            telemetry_age_seconds=0.01,
            telemetry_fresh=True,
        )
        self.assertEqual(after_reset["has_temporal_context"], 0.0)

    def test_live_dataset_validation_and_explicit_baseline_curation(self) -> None:
        session_id = "session-baseline"
        live = _live_records(session_id)
        curated = curate_baseline_session(
            live,
            bridge_session_id=session_id,
            minimum_stack_high_water_words=96,
        )
        self.assertEqual(len(curated), len(live))
        self.assertTrue(all(record["training_eligible"] for record in curated))
        self.assertTrue(
            all(
                record["curation"]["labels_used_for_training"] is False
                for record in curated
            )
        )
        uncurated = dict(curated[0])
        del uncurated["curation"]
        with self.assertRaisesRegex(
            EmbeddedMLDataError, "explicit baseline curation"
        ):
            validate_dataset_record(uncurated)
        malformed = dict(live[0])
        malformed["task_id"] = "host-worker"
        with self.assertRaises(EmbeddedMLDataError):
            validate_dataset_record(malformed)

    def test_scenarios_are_explicitly_simulated_and_not_training_data(self) -> None:
        live = _live_records("session-scenarios", count_per_task=4)
        scenarios = generate_simulated_scenarios(
            live,
            minimum_stack_high_water_words=96,
        )
        self.assertEqual(len(scenarios), 2 * 4 * 5)
        self.assertEqual(
            {record["data_source"] for record in scenarios},
            {"simulated_scenario"},
        )
        self.assertTrue(all(record["training_eligible"] is False for record in scenarios))
        self.assertEqual(
            {record["scenario_type"] for record in scenarios},
            {
                "normal_operation",
                "gradual_stack_reduction",
                "sudden_stack_reduction",
                "stalled_progress",
                "recovery_progress",
            },
        )
        self.assertTrue(
            any(record["reference_condition"] for record in scenarios)
        )

    def test_model_training_inference_and_held_out_group_checks(self) -> None:
        first_session = "training-session-a"
        second_session = "training-session-b"
        curated = []
        for session_id in (first_session, second_session):
            curated.extend(
                curate_baseline_session(
                    _live_records(session_id),
                    bridge_session_id=session_id,
                    minimum_stack_high_water_words=96,
                )
            )

        with tempfile.TemporaryDirectory() as temporary:
            model_path = Path(temporary) / "embedded.joblib"
            trained = train_embedded_model(curated, model_path)
            self.assertEqual(trained["record_count"], 48)
            artifact = load_embedded_model(model_path)
            self.assertEqual(
                set(artifact["training_manifest"]["source_groups"]),
                {first_session, second_session},
            )
            sample = _live_records("inference-session", count_per_task=1)[0]
            stale = infer_embedded_anomaly(
                artifact,
                task_id=sample["task_id"],
                observed_at_utc=sample["observed_at_utc"],
                telemetry_fresh=False,
                features=sample["features"],
            )
            self.assertEqual(stale["status"], "unavailable")
            self.assertIn("stale", stale["reason"])
            with self.assertRaises(FileExistsError):
                train_embedded_model(curated, model_path)

            held_out = _live_records("evaluation-session", count_per_task=4)
            simulated = generate_simulated_scenarios(
                held_out,
                minimum_stack_high_water_words=96,
            )
            shadow = EmbeddedMLShadowMode(
                model_path=model_path,
                data_dir=Path(temporary) / "shadow",
            )
            first = held_out[0]
            inference = shadow.ingest(
                first["raw_telemetry"],
                bridge_session_id="evaluation-session",
                observed_at_utc=first["observed_at_utc"],
                assessment={
                    "telemetry_freshness": "fresh",
                    "risk_category": "LOW",
                    "risk_index": 0,
                    "recommendation": "none",
                    "reason": "fresh reserve exceeds configured threshold",
                    "observed_inputs": {"telemetry_age_seconds": 0.01},
                },
            )
            self.assertIn(inference["ml_assessment"]["status"], {"available"})
            self.assertFalse(inference["checkpoint_command_issued"])
            self.assertFalse(shadow.snapshot()["checkpoint_commands_enabled"])

            runtime = MonitorRuntime(
                workers=1,
                memory_mib=1,
                sample_interval=0.1,
                checkpoint_interval=0.2,
                checkpoint_dir=Path(temporary) / "runtime-checkpoints",
                risk_model_path=Path(temporary) / "missing-host-model.joblib",
                embedded_stack_reserve_threshold_words=96,
                embedded_data_dir=Path(temporary) / "runtime-data",
                embedded_ml_model_path=model_path,
            )
            runtime.start()
            try:
                runtime.ingest_embedded_telemetry(
                    str(uuid.uuid4()),
                    held_out[0]["raw_telemetry"],
                )
                runtime_snapshot = runtime.snapshot()
                integrated_ml = runtime_snapshot["embedded_ml"]["latest_by_task"][0]
                self.assertEqual(integrated_ml["ml_assessment"]["status"], "available")
                self.assertFalse(integrated_ml["checkpoint_command_issued"])
                sensor_risk = next(
                    item
                    for item in runtime_snapshot["embedded_risk_assessments"]
                    if item["task_id"] == "sensor-sampler"
                )
                self.assertEqual(
                    sensor_risk["risk_category"],
                    "LOW",
                )
            finally:
                runtime.stop()

            evaluation = evaluate_embedded_policy(
                simulated,
                artifact,
                minimum_stack_high_water_words=96,
            )
            self.assertTrue(evaluation["metrics_supported"])
            self.assertFalse(evaluation["labels_are_failure_labels"])

            training_group_scenarios = generate_simulated_scenarios(
                _live_records(first_session, count_per_task=4),
                minimum_stack_high_water_words=96,
            )
            with self.assertRaises(EmbeddedMLDataError):
                evaluate_embedded_policy(
                    training_group_scenarios,
                    artifact,
                    minimum_stack_high_water_words=96,
                )

    def test_missing_model_is_reported_without_claiming_low_risk(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing.joblib"
            with self.assertRaises(EmbeddedModelError):
                load_embedded_model(missing)
            shadow = EmbeddedMLShadowMode(
                model_path=missing,
                data_dir=Path(temporary) / "shadow",
            )
            snapshot = shadow.snapshot()
            self.assertFalse(snapshot["model"]["available"])
            self.assertIn("not found", snapshot["model"]["reason"])

    def test_incompatible_model_schema_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            incompatible = Path(temporary) / "incompatible.joblib"
            joblib.dump({"artifact_schema_version": 1, "feature_schema_version": 99}, incompatible)
            with self.assertRaisesRegex(EmbeddedModelError, "feature schema"):
                load_embedded_model(incompatible)


if __name__ == "__main__":
    unittest.main()
