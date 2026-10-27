"""Host-telemetry feature extraction and Isolation Forest risk inference."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping

from twincoreos.digital_twin import HealthState, TwinState
from twincoreos.risk_predictor import (
    BaselineRiskPredictor,
    InvalidRiskInputError,
    RiskAssessment,
    RiskCategory,
    RiskPredictor,
    RiskThresholds,
    mark_risk_assessment_warming_up,
)

logger = logging.getLogger(__name__)

ARTIFACT_VERSION = 1
MINIMUM_TRAINING_SAMPLES = 20
DEFAULT_MODEL_PATH = Path(".twincoreos/models/host-isolation-forest.joblib")

FEATURE_NAMES = (
    "cpu_utilization_percent_one_core",
    "memory_rss_bytes",
    "cpu_trend_percent_points",
    "memory_rss_trend_bytes",
    "telemetry_gap_seconds",
    "cpu_trend_missing",
    "rss_trend_missing",
    "telemetry_gap_missing",
    "telemetry_stale",
    "health_healthy",
    "health_degraded",
    "health_failed",
    "cpu_missing",
    "rss_missing",
    "measurement_error_count",
    "process_state_changed",
)


def _number(value: Any, name: str, *, integer: bool = False) -> float | int | None:
    if value is None:
        return None
    valid = (
        isinstance(value, int) and not isinstance(value, bool)
        if integer
        else isinstance(value, (int, float)) and not isinstance(value, bool)
    )
    if not valid:
        raise InvalidRiskInputError(
            f"{name} must be finite, non-negative, or null"
        )
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or value < 0:
        raise InvalidRiskInputError(
            f"{name} must be finite, non-negative, or null"
        )
    return value


def _float_feature(value: float | int | None, name: str) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except OverflowError as error:
        raise InvalidRiskInputError(f"{name} is outside the model's numeric range") from error
    if not math.isfinite(result):
        raise InvalidRiskInputError(f"{name} is outside the model's numeric range")
    return result


def _capture_time(observation: Mapping[str, Any] | None) -> datetime | None:
    if observation is None:
        return None
    value = observation.get("captured_at_utc")
    if not isinstance(value, str):
        raise InvalidRiskInputError("captured_at_utc must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise InvalidRiskInputError("captured_at_utc is invalid") from error
    if parsed.tzinfo is None:
        raise InvalidRiskInputError("captured_at_utc must include a timezone")
    return parsed.astimezone(timezone.utc)


def extract_features(twin: TwinState) -> tuple[list[float], dict[str, Any]]:
    """Return the fixed-order model vector and a readable observation summary."""
    if not isinstance(twin, TwinState):
        raise InvalidRiskInputError("twin must be a Digital Twin TwinState")
    if not twin.worker_id or twin.pid <= 0:
        raise InvalidRiskInputError("twin identity must include worker ID and PID")
    if not isinstance(twin.health_state, HealthState):
        raise InvalidRiskInputError("health_state is invalid")
    if not isinstance(twin.stale, bool):
        raise InvalidRiskInputError("stale must be a boolean")

    latest = twin.latest_observation
    previous = twin.previous_observation
    if latest is not None and not isinstance(latest, dict):
        raise InvalidRiskInputError("latest_observation must be an object or null")
    if previous is not None and not isinstance(previous, dict):
        raise InvalidRiskInputError("previous_observation must be an object or null")

    cpu = _number(
        latest.get("cpu_utilization_percent_one_core") if latest else None,
        "cpu_utilization_percent_one_core",
    )
    rss = _number(
        latest.get("memory_rss_bytes") if latest else None,
        "memory_rss_bytes",
        integer=True,
    )
    prior_cpu = _number(
        previous.get("cpu_utilization_percent_one_core") if previous else None,
        "previous cpu_utilization_percent_one_core",
    )
    prior_rss = _number(
        previous.get("memory_rss_bytes") if previous else None,
        "previous memory_rss_bytes",
        integer=True,
    )
    cpu_trend = (
        cpu - prior_cpu
        if cpu is not None and prior_cpu is not None
        else None
    )
    rss_trend = (
        rss - prior_rss
        if rss is not None and prior_rss is not None
        else None
    )
    cpu_feature = _float_feature(cpu, "cpu_utilization_percent_one_core")
    rss_feature = _float_feature(rss, "memory_rss_bytes")
    cpu_trend_feature = _float_feature(
        cpu_trend, "cpu_trend_percent_points"
    )
    rss_trend_feature = _float_feature(
        rss_trend, "memory_rss_trend_bytes"
    )
    latest_status = latest.get("process_status") if latest else None
    previous_status = previous.get("process_status") if previous else None
    if latest_status is not None and not isinstance(latest_status, str):
        raise InvalidRiskInputError("process_status must be a string or null")
    if previous_status is not None and not isinstance(previous_status, str):
        raise InvalidRiskInputError("previous process_status must be a string or null")
    status_changed = (
        previous is not None
        and latest_status != previous_status
    )
    errors = latest.get("measurement_errors", {}) if latest else {}
    if not isinstance(errors, Mapping):
        raise InvalidRiskInputError("measurement_errors must be an object")
    interval = _number(
        latest.get("cpu_interval_seconds") if latest else None,
        "cpu_interval_seconds",
    )
    latest_time = _capture_time(latest)
    previous_time = _capture_time(previous)
    telemetry_gap = (
        (latest_time - previous_time).total_seconds()
        if latest_time is not None and previous_time is not None
        else interval
    )
    if telemetry_gap is not None and telemetry_gap < 0:
        raise InvalidRiskInputError("telemetry timestamps must be ordered")

    feature_values: dict[str, float | None] = {
        "cpu_utilization_percent_one_core": cpu_feature,
        "memory_rss_bytes": rss_feature,
        "cpu_trend_percent_points": cpu_trend_feature,
        "memory_rss_trend_bytes": rss_trend_feature,
        "telemetry_gap_seconds": telemetry_gap,
        "cpu_trend_missing": float(cpu_trend is None),
        "rss_trend_missing": float(rss_trend is None),
        "telemetry_gap_missing": float(telemetry_gap is None),
        "telemetry_stale": float(twin.stale),
        "health_healthy": float(twin.health_state == HealthState.HEALTHY),
        "health_degraded": float(twin.health_state == HealthState.DEGRADED),
        "health_failed": float(twin.health_state == HealthState.FAILED),
        "cpu_missing": float(cpu is None),
        "rss_missing": float(rss is None),
        "measurement_error_count": float(len(errors)),
        "process_state_changed": float(status_changed),
    }
    vector = [
        math.nan if feature_values[name] is None else feature_values[name]
        for name in FEATURE_NAMES
    ]
    missing = []
    if cpu is None:
        missing.append("cpu_utilization_percent_one_core")
    if rss is None:
        missing.append("memory_rss_bytes")
    summary: dict[str, Any] = {
        **feature_values,
        "health_state": twin.health_state.value,
        "telemetry_stale": twin.stale,
        "missing_measurements": missing,
        "measurement_errors": dict(errors),
        "process_status": latest_status,
        "previous_process_status": previous_status,
        "process_state_changed": status_changed,
        "observed_worker_failure": twin.health_state == HealthState.FAILED,
    }
    return vector, summary


def _validate_category_scores(normal_risk_score: float, anomaly_risk_score: float) -> None:
    for name, score in (
        ("normal_risk_score", normal_risk_score),
        ("anomaly_risk_score", anomaly_risk_score),
    ):
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(score)
            or not 0 <= score <= 1
        ):
            raise ValueError(f"{name} must be between 0 and 1")
    if anomaly_risk_score < normal_risk_score:
        raise ValueError("anomaly_risk_score must be >= normal_risk_score")


class IsolationForestRiskPredictor(RiskPredictor):
    """Anomaly detector; its output is not an estimated failure probability."""

    def __init__(
        self,
        pipeline: Any,
        *,
        normal_risk_score: float = 0.0,
        anomaly_risk_score: float = 0.60,
        thresholds: RiskThresholds | None = None,
        assessment_validity_seconds: float = 5.0,
    ) -> None:
        _validate_category_scores(normal_risk_score, anomaly_risk_score)
        if (
            not math.isfinite(assessment_validity_seconds)
            or assessment_validity_seconds <= 0
        ):
            raise ValueError("assessment_validity_seconds must be positive")
        if not callable(getattr(pipeline, "decision_function", None)):
            raise ValueError("model must provide decision_function")
        self.pipeline = pipeline
        self.normal_risk_score = float(normal_risk_score)
        self.anomaly_risk_score = float(anomaly_risk_score)
        self.thresholds = thresholds or RiskThresholds()
        self.assessment_validity_seconds = assessment_validity_seconds
        self._previous: dict[str, RiskAssessment] = {}

    def assess(
        self,
        twin: TwinState,
        assessed_at: datetime | None = None,
    ) -> RiskAssessment:
        now = datetime.now(timezone.utc) if assessed_at is None else assessed_at
        if now.tzinfo is None:
            raise InvalidRiskInputError("assessed_at must include a timezone")
        now = now.astimezone(timezone.utc)
        if twin.previous_observation is None:
            assessment = BaselineRiskPredictor(
                assessment_validity_seconds=self.assessment_validity_seconds
            ).assess(twin, now)
            return mark_risk_assessment_warming_up(assessment)
        vector, features = extract_features(twin)
        try:
            decision = float(self.pipeline.decision_function([vector])[0])
        except Exception as error:
            raise RuntimeError(
                f"Isolation Forest inference failed: {error}"
            ) from error
        if not math.isfinite(decision):
            raise RuntimeError("Isolation Forest returned a non-finite decision score")
        anomalous = decision < 0
        risk_score = (
            self.anomaly_risk_score if anomalous else self.normal_risk_score
        )
        if risk_score < self.thresholds.low_category_max:
            category = RiskCategory.LOW
        elif risk_score < self.thresholds.medium_category_max:
            category = RiskCategory.MEDIUM
        else:
            category = RiskCategory.HIGH

        prior = self._previous.get(twin.worker_id)
        if prior is not None and prior.pid != twin.pid:
            raise InvalidRiskInputError(
                f"worker {twin.worker_id} PID changed without a new identity"
            )
        features.update(
            {
                "model_type": "isolation_forest",
                "anomaly_score": -decision,
                "anomaly_status": "anomalous" if anomalous else "inlier",
                "anomaly_decision_threshold": 0.0,
                "risk_score_semantics": (
                    "configured category-compatibility index; not a failure probability"
                ),
            }
        )
        explanation = (
            "Isolation Forest marked this observation as anomalous "
            f"(signed boundary score {-decision:.6g}); this indicates statistical "
            "deviation from fitted host telemetry, not a failure probability or cause."
            if anomalous
            else "Isolation Forest marked this observation as an inlier; this does not "
            "guarantee worker health or estimate failure probability."
        )
        assessment = RiskAssessment(
            worker_id=twin.worker_id,
            pid=twin.pid,
            risk_score=risk_score,
            risk_category=category,
            observed_features=features,
            explanation=explanation,
            assessed_at_utc=now.isoformat(),
            valid_until_utc=(
                now + timedelta(seconds=self.assessment_validity_seconds)
            ).isoformat(),
            previous_category=prior.risk_category if prior is not None else None,
            category_changed=(
                prior is not None and prior.risk_category != category
            ),
        )
        self._previous[twin.worker_id] = assessment
        return assessment


class FallbackRiskPredictor(RiskPredictor):
    """Use ML when available, otherwise explicitly annotate baseline results."""

    def __init__(
        self,
        predictor: RiskPredictor | None,
        *,
        assessment_validity_seconds: float,
        fallback_reason: str | None = None,
    ) -> None:
        self.predictor = predictor
        self.baseline = BaselineRiskPredictor(
            assessment_validity_seconds=assessment_validity_seconds
        )
        self.fallback_reason = fallback_reason
        self._inference_failure_logged = False

    def assess(
        self,
        twin: TwinState,
        assessed_at: datetime | None = None,
    ) -> RiskAssessment:
        if not isinstance(twin, TwinState):
            return self.baseline.assess(twin, assessed_at)
        if twin.health_state == HealthState.FAILED:
            assessment = self.baseline.assess(twin, assessed_at)
            assessment.observed_features.update(
                {
                    "model_type": "baseline_failure_override",
                    "ml_inference_skipped_reason": "confirmed worker failure",
                }
            )
            return assessment

        if twin.previous_observation is None:
            return mark_risk_assessment_warming_up(
                self.baseline.assess(twin, assessed_at)
            )

        if self.predictor is not None:
            try:
                return self.predictor.assess(twin, assessed_at)
            except Exception as error:
                if not self._inference_failure_logged:
                    logger.exception(
                        "ML risk inference failed for worker %s; using baseline",
                        twin.worker_id,
                    )
                    self._inference_failure_logged = True
                self.fallback_reason = f"{type(error).__name__}: {error}"

        assessment = self.baseline.assess(twin, assessed_at)
        assessment.observed_features.update(
            {
                "model_type": "baseline_fallback",
                "fallback_reason": self.fallback_reason or "ML model unavailable",
            }
        )
        assessment.explanation = (
            "Baseline heuristic used because ML inference is unavailable: "
            f"{self.fallback_reason or 'ML model unavailable'}. "
            + assessment.explanation
        )
        return assessment


def create_risk_predictor(
    model_path: str | Path = DEFAULT_MODEL_PATH,
    *,
    assessment_validity_seconds: float = 5.0,
    normal_risk_score: float = 0.0,
    anomaly_risk_score: float = 0.60,
) -> FallbackRiskPredictor:
    _validate_category_scores(normal_risk_score, anomaly_risk_score)
    path = Path(model_path)
    try:
        if not path.is_file():
            raise FileNotFoundError(f"model artifact not found: {path}")
        predictor = load_isolation_forest(
            path,
            normal_risk_score=normal_risk_score,
            anomaly_risk_score=anomaly_risk_score,
            assessment_validity_seconds=assessment_validity_seconds,
        )
        logger.info("Loaded host telemetry Isolation Forest from %s", path)
        return FallbackRiskPredictor(
            predictor, assessment_validity_seconds=assessment_validity_seconds
        )
    except Exception as error:
        reason = f"{type(error).__name__}: {error}"
        logger.warning("ML risk model unavailable; using baseline predictor: %s", reason)
        return FallbackRiskPredictor(
            None,
            assessment_validity_seconds=assessment_validity_seconds,
            fallback_reason=reason,
        )


def load_isolation_forest(
    path: str | Path,
    *,
    normal_risk_score: float = 0.0,
    anomaly_risk_score: float = 0.60,
    assessment_validity_seconds: float = 5.0,
) -> IsolationForestRiskPredictor:
    import joblib
    import sklearn
    from sklearn.ensemble import IsolationForest
    from sklearn.impute import SimpleImputer
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    artifact = joblib.load(path)
    if not isinstance(artifact, dict):
        raise ValueError("model artifact must be a dictionary")
    if artifact.get("artifact_version") != ARTIFACT_VERSION:
        raise ValueError("unsupported model artifact version")
    if artifact.get("model_type") != "isolation_forest":
        raise ValueError("model artifact type is not isolation_forest")
    if artifact.get("feature_names") != list(FEATURE_NAMES):
        raise ValueError("model artifact feature schema does not match this version")
    if artifact.get("scikit_learn_version") != sklearn.__version__:
        raise ValueError("model artifact scikit-learn version does not match runtime")
    pipeline = artifact.get("pipeline")
    if not isinstance(pipeline, Pipeline):
        raise ValueError("model artifact pipeline is invalid")
    if tuple(pipeline.named_steps) != ("imputer", "scaler", "isolation_forest"):
        raise ValueError("model artifact preprocessing pipeline is incomplete")
    if not isinstance(pipeline.named_steps["imputer"], SimpleImputer):
        raise ValueError("model artifact imputer is invalid")
    if not isinstance(pipeline.named_steps["scaler"], StandardScaler):
        raise ValueError("model artifact scaler is invalid")
    forest = pipeline.named_steps["isolation_forest"]
    if not isinstance(forest, IsolationForest):
        raise ValueError("model artifact estimator is not an Isolation Forest")
    if not getattr(forest, "estimators_", None):
        raise ValueError("model artifact has not been fitted")
    if getattr(pipeline, "n_features_in_", None) != len(FEATURE_NAMES):
        raise ValueError("model artifact has an invalid feature count")
    return IsolationForestRiskPredictor(
        pipeline,
        normal_risk_score=normal_risk_score,
        anomaly_risk_score=anomaly_risk_score,
        assessment_validity_seconds=assessment_validity_seconds,
    )


def telemetry_feature_rows(
    records: Iterable[Mapping[str, Any]],
) -> list[list[float]]:
    """Rebuild ordered per-process twins and extract only host telemetry rows."""
    from twincoreos.digital_twin import DigitalTwinEngine

    streams: dict[tuple[str, int, str], list[Mapping[str, Any]]] = {}
    for line_number, record in enumerate(records, start=1):
        if record.get("type") != "telemetry":
            continue
        worker_id = record.get("task_id")
        pid = record.get("pid")
        created = record.get("process_created_at_utc")
        if (
            not isinstance(worker_id, str)
            or not worker_id
            or not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 0
            or not isinstance(created, str)
        ):
            raise ValueError(f"telemetry record {line_number} has invalid identity")
        streams.setdefault((worker_id, pid, created), []).append(record)

    result: list[list[float]] = []
    for stream_records in streams.values():
        try:
            ordered = sorted(
                stream_records,
                key=lambda record: datetime.fromisoformat(
                    record["captured_at_utc"]
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("telemetry record has an invalid captured_at_utc") from error
        engine = DigitalTwinEngine()
        for index, record in enumerate(ordered, start=1):
            twin = engine.update(record, observed_at_monotonic=float(index))
            vector, _summary = extract_features(twin)
            result.append(vector)
    return result


def train_isolation_forest(
    rows: list[list[float]],
    output_path: str | Path,
    *,
    contamination: float = 0.05,
    random_state: int = 42,
) -> dict[str, Any]:
    if len(rows) < MINIMUM_TRAINING_SAMPLES:
        raise ValueError(
            f"at least {MINIMUM_TRAINING_SAMPLES} real telemetry samples are required"
        )
    if any(len(row) != len(FEATURE_NAMES) for row in rows):
        raise ValueError("training row does not match the fixed feature schema")
    if (
        isinstance(contamination, bool)
        or not isinstance(contamination, (int, float))
        or not math.isfinite(contamination)
        or not 0 < contamination <= 0.5
    ):
        raise ValueError("contamination must be in (0, 0.5]")
    from sklearn.ensemble import IsolationForest
    from sklearn.impute import SimpleImputer
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    import sklearn

    pipeline = Pipeline(
        [
            (
                "imputer",
                SimpleImputer(strategy="median", keep_empty_features=True),
            ),
            ("scaler", StandardScaler()),
            (
                "isolation_forest",
                IsolationForest(
                    contamination=float(contamination),
                    random_state=random_state,
                    n_jobs=1,
                ),
            ),
        ]
    )
    pipeline.fit(rows)
    artifact = {
        "artifact_version": ARTIFACT_VERSION,
        "model_type": "isolation_forest",
        "scikit_learn_version": sklearn.__version__,
        "feature_names": list(FEATURE_NAMES),
        "contamination": float(contamination),
        "random_state": random_state,
        "pipeline": pipeline,
    }
    import joblib

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
        joblib.dump(artifact, temporary_name)
        os.replace(temporary_name, destination)
    finally:
        if temporary_name is not None and os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return {
        "model_type": "isolation_forest",
        "feature_count": len(FEATURE_NAMES),
        "training_samples": len(rows),
        "contamination": float(contamination),
        "output_path": str(destination),
    }


def read_jsonl_telemetry(path: str | Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"{path}:{line_number}: invalid JSON record"
                ) from error
            if not isinstance(record, dict):
                raise ValueError(f"{path}:{line_number}: record must be a JSON object")
            if record.get("type") == "telemetry":
                records.append(record)
    return records
