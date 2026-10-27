"""Transparent baseline risk estimation from host telemetry and Digital Twins.

Scores are heuristic risk indicators, not calibrated probabilities or outputs
from a trained machine-learning model.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import math
from typing import Any

from twincoreos.digital_twin import HealthState, TwinState


class RiskCategory(str, Enum):
    WARMING_UP = "WARMING_UP"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class InvalidRiskInputError(ValueError):
    """Raised when a Twin contains invalid predictor inputs."""


@dataclass(frozen=True)
class RiskThresholds:
    cpu_elevated_percent: float = 75.0
    cpu_high_percent: float = 90.0
    cpu_increase_percent_points: float = 10.0
    rss_increase_bytes: int = 5 * 1024 * 1024
    low_category_max: float = 0.25
    medium_category_max: float = 0.60
    cpu_elevated_score: float = 0.12
    cpu_high_score: float = 0.30
    cpu_trend_score: float = 0.20
    rss_trend_score: float = 0.20
    degraded_score: float = 0.25
    stale_score: float = 0.20
    missing_measurement_score: float = 0.10
    process_state_change_score: float = 0.10

    def __post_init__(self) -> None:
        for name in (
            "cpu_elevated_percent",
            "cpu_high_percent",
            "cpu_increase_percent_points",
            "rss_increase_bytes",
            "low_category_max",
            "medium_category_max",
            "cpu_elevated_score",
            "cpu_high_score",
            "cpu_trend_score",
            "rss_trend_score",
            "degraded_score",
            "stale_score",
            "missing_measurement_score",
            "process_state_change_score",
        ):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.cpu_high_percent < self.cpu_elevated_percent:
            raise ValueError("cpu_high_percent must be >= cpu_elevated_percent")
        if self.rss_increase_bytes <= 0 or self.cpu_increase_percent_points <= 0:
            raise ValueError("trend thresholds must be positive")
        if not 0 < self.low_category_max < self.medium_category_max < 1:
            raise ValueError("category cutoffs must satisfy 0 < low < medium < 1")
        if any(
            getattr(self, name) > 1
            for name in (
                "cpu_elevated_score",
                "cpu_high_score",
                "cpu_trend_score",
                "rss_trend_score",
                "degraded_score",
                "stale_score",
                "missing_measurement_score",
                "process_state_change_score",
            )
        ):
            raise ValueError("individual score contributions must not exceed 1")


@dataclass
class RiskAssessment:
    worker_id: str
    pid: int
    risk_score: float | None
    risk_category: RiskCategory
    observed_features: dict[str, Any]
    explanation: str
    assessed_at_utc: str
    valid_until_utc: str
    previous_category: RiskCategory | None
    category_changed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "risk_assessment",
            "worker_id": self.worker_id,
            "pid": self.pid,
            "risk_score": self.risk_score,
            "risk_category": self.risk_category.value,
            "observed_features": dict(self.observed_features),
            "explanation": self.explanation,
            "assessed_at_utc": self.assessed_at_utc,
            "valid_until_utc": self.valid_until_utc,
            "previous_category": (
                self.previous_category.value
                if self.previous_category is not None
                else None
            ),
            "category_changed": self.category_changed,
        }

    def is_valid(self, now: datetime | None = None) -> bool:
        current = datetime.now(timezone.utc) if now is None else now
        if current.tzinfo is None:
            raise ValueError("now must include a timezone")
        return current.astimezone(timezone.utc) <= datetime.fromisoformat(
            self.valid_until_utc
        )


def mark_risk_assessment_warming_up(
    assessment: RiskAssessment,
) -> RiskAssessment:
    assessment.risk_score = None
    assessment.risk_category = RiskCategory.WARMING_UP
    assessment.observed_features["model_type"] = "warming_up"
    assessment.observed_features["risk_score_semantics"] = (
        "unavailable until at least two telemetry samples are observed"
    )
    assessment.explanation = (
        "Risk assessment is warming up; at least two telemetry samples are "
        "required to assess per-worker trends."
    )
    assessment.category_changed = (
        assessment.previous_category is not None
        and assessment.previous_category != RiskCategory.WARMING_UP
    )
    return assessment


class RiskPredictor:
    """Replaceable interface for risk assessment implementations."""

    def assess(
        self,
        twin: TwinState,
        assessed_at: datetime | None = None,
    ) -> RiskAssessment:
        raise NotImplementedError


class BaselineRiskPredictor(RiskPredictor):
    """Deterministic, uncalibrated rule-based baseline risk estimator.

    Contributions are additive and capped at 1.0: high CPU (+0.30), elevated
    CPU (+0.12), increasing CPU (+0.20), increasing RSS (+0.20), DEGRADED
    health (+0.25), stale telemetry (+0.20), each missing CPU/RSS measure
    (+0.10), recent process-state change (+0.10), and FAILED health (score 1).
    The highest applicable CPU rule is used, not both. Category cutoffs are
    LOW below 0.25, MEDIUM from 0.25 to below 0.60, and HIGH at 0.60 or above.
    """

    def __init__(
        self,
        thresholds: RiskThresholds | None = None,
        assessment_validity_seconds: float = 5.0,
    ) -> None:
        if (
            not isinstance(assessment_validity_seconds, (int, float))
            or not math.isfinite(assessment_validity_seconds)
            or assessment_validity_seconds <= 0
        ):
            raise ValueError("assessment_validity_seconds must be finite and positive")
        self.thresholds = thresholds or RiskThresholds()
        self.assessment_validity_seconds = float(assessment_validity_seconds)
        self._previous: dict[str, RiskAssessment] = {}

    @staticmethod
    def _metric(
        value: Any,
        label: str,
        *,
        integer: bool = False,
    ) -> int | float | None:
        if value is None:
            return None
        valid_type = (
            isinstance(value, int) and not isinstance(value, bool)
            if integer
            else isinstance(value, (int, float)) and not isinstance(value, bool)
        )
        if not valid_type or not math.isfinite(value) or value < 0:
            raise InvalidRiskInputError(f"{label} must be finite, non-negative, or null")
        return value

    @staticmethod
    def _observation_metric(
        observation: dict[str, Any] | None,
        key: str,
        *,
        integer: bool = False,
    ) -> int | float | None:
        if observation is None:
            return None
        return BaselineRiskPredictor._metric(
            observation.get(key), key, integer=integer
        )

    def assess(
        self,
        twin: TwinState,
        assessed_at: datetime | None = None,
    ) -> RiskAssessment:
        if not isinstance(twin, TwinState):
            raise InvalidRiskInputError("twin must be a Digital Twin TwinState")
        if not twin.worker_id or twin.pid <= 0:
            raise InvalidRiskInputError("twin identity must include worker ID and PID")

        now = datetime.now(timezone.utc) if assessed_at is None else assessed_at
        if now.tzinfo is None:
            raise InvalidRiskInputError("assessed_at must include a timezone")
        now = now.astimezone(timezone.utc)
        latest = twin.latest_observation
        previous = twin.previous_observation
        cpu = self._observation_metric(
            latest, "cpu_utilization_percent_one_core"
        )
        prior_cpu = self._observation_metric(
            previous, "cpu_utilization_percent_one_core"
        )
        rss = self._observation_metric(latest, "memory_rss_bytes", integer=True)
        prior_rss = self._observation_metric(
            previous, "memory_rss_bytes", integer=True
        )
        cpu_trend = cpu - prior_cpu if cpu is not None and prior_cpu is not None else None
        rss_trend = rss - prior_rss if rss is not None and prior_rss is not None else None
        latest_status = latest.get("process_status") if latest else None
        previous_status = previous.get("process_status") if previous else None
        process_state_changed = (
            previous is not None
            and latest_status is not None
            and previous_status is not None
            and latest_status != previous_status
        )
        errors = latest.get("measurement_errors", {}) if latest else {}
        if not isinstance(errors, dict):
            raise InvalidRiskInputError("measurement_errors must be an object")
        missing = []
        if cpu is None:
            missing.append("cpu_utilization_percent_one_core")
        if rss is None:
            missing.append("memory_rss_bytes")

        features: dict[str, Any] = {
            "cpu_utilization_percent_one_core": cpu,
            "cpu_trend_percent_points": cpu_trend,
            "memory_rss_bytes": rss,
            "memory_rss_trend_bytes": rss_trend,
            "health_state": twin.health_state.value,
            "telemetry_stale": twin.stale,
            "missing_measurements": missing,
            "measurement_errors": dict(errors),
            "process_status": latest_status,
            "previous_process_status": previous_status,
            "process_state_changed": process_state_changed,
            "observed_worker_failure": twin.health_state == HealthState.FAILED,
            "risk_score_semantics": (
                "baseline heuristic index from 0 to 1; not a failure probability"
            ),
        }

        score = 0.0
        reasons: list[str] = []
        thresholds = self.thresholds
        if twin.health_state == HealthState.FAILED:
            score = 1.0
            reasons.append("worker failure was observed")
        else:
            if cpu is not None:
                if cpu >= thresholds.cpu_high_percent:
                    score += thresholds.cpu_high_score
                    reasons.append(
                        f"CPU utilization is high ({cpu:.1f}% of one core)"
                    )
                elif cpu >= thresholds.cpu_elevated_percent:
                    score += thresholds.cpu_elevated_score
                    reasons.append(
                        f"CPU utilization is elevated ({cpu:.1f}% of one core)"
                    )
            if (
                cpu_trend is not None
                and cpu_trend >= thresholds.cpu_increase_percent_points
            ):
                score += thresholds.cpu_trend_score
                reasons.append(f"CPU utilization increased by {cpu_trend:.1f} points")
            if rss_trend is not None and rss_trend >= thresholds.rss_increase_bytes:
                score += thresholds.rss_trend_score
                reasons.append(f"RSS increased by {rss_trend} bytes")
            if twin.health_state == HealthState.DEGRADED:
                score += thresholds.degraded_score
                reasons.append("Digital Twin health is DEGRADED")
            if twin.stale:
                score += thresholds.stale_score
                reasons.append("latest telemetry is stale")
            for name in missing:
                score += thresholds.missing_measurement_score
                reasons.append(f"{name} is unavailable")
            if process_state_changed:
                score += thresholds.process_state_change_score
                reasons.append(
                    f"process status changed from {previous_status} to {latest_status}"
                )
        score = min(1.0, max(0.0, score))
        if score < thresholds.low_category_max:
            category = RiskCategory.LOW
        elif score < thresholds.medium_category_max:
            category = RiskCategory.MEDIUM
        else:
            category = RiskCategory.HIGH

        previous_assessment = self._previous.get(twin.worker_id)
        if previous_assessment is not None and previous_assessment.pid != twin.pid:
            raise InvalidRiskInputError(
                f"worker {twin.worker_id} PID changed without a new identity"
            )
        assessment = RiskAssessment(
            worker_id=twin.worker_id,
            pid=twin.pid,
            risk_score=score,
            risk_category=category,
            observed_features=features,
            explanation="; ".join(reasons) if reasons else "no elevated risk indicators observed",
            assessed_at_utc=now.isoformat(),
            valid_until_utc=(
                now + timedelta(seconds=self.assessment_validity_seconds)
            ).isoformat(),
            previous_category=(
                previous_assessment.risk_category
                if previous_assessment is not None
                else None
            ),
            category_changed=(
                previous_assessment is not None
                and previous_assessment.risk_category != category
            ),
        )
        self._previous[twin.worker_id] = assessment
        return assessment
