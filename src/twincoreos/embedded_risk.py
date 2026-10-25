"""Heuristic operational-risk assessments for observed FreeRTOS telemetry."""

from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any, Mapping


EMBEDDED_TASK_IDS = frozenset({"sensor-sampler", "control-loop"})
EMBEDDED_CHECKPOINT_TASK_IDS = frozenset({"sensor-sampler"})
EMBEDDED_TARGET = "freertos-qemu-rv32"
EMBEDDED_TASK_STATES = frozenset(
    {"running", "ready", "blocked", "suspended", "deleted"}
)


class EmbeddedRiskPolicy:
    """Assess stack reserve and telemetry freshness without claiming failure probability."""

    def __init__(
        self,
        *,
        minimum_stack_high_water_words: int = 32,
        stale_after_seconds: float = 3.0,
    ) -> None:
        if (
            not isinstance(minimum_stack_high_water_words, int)
            or isinstance(minimum_stack_high_water_words, bool)
            or minimum_stack_high_water_words < 0
        ):
            raise ValueError("minimum_stack_high_water_words must be non-negative")
        if (
            not isinstance(stale_after_seconds, (int, float))
            or isinstance(stale_after_seconds, bool)
            or not math.isfinite(stale_after_seconds)
            or stale_after_seconds <= 0
        ):
            raise ValueError("stale_after_seconds must be finite and positive")
        self.minimum_stack_high_water_words = minimum_stack_high_water_words
        self.stale_after_seconds = float(stale_after_seconds)

    def assess(
        self,
        task_id: str,
        sample: Mapping[str, Any] | None,
        *,
        assessed_at: datetime | None = None,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc) if assessed_at is None else assessed_at
        if now.tzinfo is None:
            raise ValueError("assessed_at must include a timezone")
        now = now.astimezone(timezone.utc)
        assessed_at_utc = now.isoformat()
        assessment: dict[str, Any] = {
            "assessment_type": "heuristic_operational_risk",
            "policy_id": "embedded-stack-reserve-v1",
            "target": EMBEDDED_TARGET,
            "task_id": task_id,
            "risk_level": "unknown",
            "recommendation": "none",
            "matched_rules": [],
            "observed_inputs": {},
            "assessed_at_utc": assessed_at_utc,
            "limitations": [
                "This is an operational heuristic, not a trained or calibrated failure predictor."
            ],
        }
        if not isinstance(task_id, str) or task_id not in EMBEDDED_TASK_IDS:
            assessment["reason"] = "unsupported embedded task identity"
            return assessment
        if task_id not in EMBEDDED_CHECKPOINT_TASK_IDS:
            assessment["reason"] = (
                "application-level checkpointing is not implemented for this task"
            )
            return assessment
        if sample is None:
            assessment["reason"] = "no embedded telemetry has been received"
            return assessment
        if not isinstance(sample, Mapping):
            assessment["reason"] = "embedded telemetry is malformed"
            return assessment

        if (
            sample.get("target") != EMBEDDED_TARGET
            or sample.get("task_id") != task_id
            or sample.get("message_type") != "task_telemetry"
            or not isinstance(sample.get("schema_version"), int)
            or isinstance(sample.get("schema_version"), bool)
            or sample.get("schema_version") != 1
        ):
            assessment["reason"] = "embedded telemetry identity or schema is invalid"
            return assessment

        received_at = sample.get("received_at_utc")
        if not isinstance(received_at, str):
            assessment["reason"] = "backend receipt time is unavailable"
            return assessment
        try:
            received_time = datetime.fromisoformat(received_at)
        except ValueError:
            assessment["reason"] = "backend receipt time is invalid"
            return assessment
        if received_time.tzinfo is None:
            assessment["reason"] = "backend receipt time lacks a timezone"
            return assessment
        age_seconds = (now - received_time.astimezone(timezone.utc)).total_seconds()
        if not math.isfinite(age_seconds) or age_seconds < 0:
            assessment["reason"] = "backend receipt time is in the future or invalid"
            return assessment

        required_fields = (
            "task_state",
            "tick_count",
            "execution_count",
            "stack_high_water_mark_words",
        )
        if any(field not in sample for field in required_fields):
            assessment["reason"] = "embedded telemetry is missing required measurements"
            return assessment
        if (
            not isinstance(sample["task_state"], str)
            or sample["task_state"] not in EMBEDDED_TASK_STATES
            or not isinstance(sample["tick_count"], int)
            or isinstance(sample["tick_count"], bool)
            or not isinstance(sample["execution_count"], int)
            or isinstance(sample["execution_count"], bool)
            or not isinstance(sample["stack_high_water_mark_words"], int)
            or isinstance(sample["stack_high_water_mark_words"], bool)
            or any(
                (
                    not 0 <= sample[field] <= 0xFFFFFFFF
                    or (field == "execution_count" and sample[field] < 1)
                )
                for field in (
                    "tick_count",
                    "execution_count",
                    "stack_high_water_mark_words",
                )
            )
        ):
            assessment["reason"] = "embedded telemetry contains invalid measurement values"
            return assessment

        assessment["observed_inputs"] = {
            "task_state": sample["task_state"],
            "tick_count": sample["tick_count"],
            "execution_count": sample["execution_count"],
            "stack_high_water_mark_words": sample["stack_high_water_mark_words"],
            "received_at_utc": received_time.astimezone(timezone.utc).isoformat(),
            "telemetry_age_seconds": age_seconds,
        }
        assessment["policy_configuration"] = {
            "minimum_stack_high_water_words": self.minimum_stack_high_water_words,
            "stale_after_seconds": self.stale_after_seconds,
        }

        if age_seconds > self.stale_after_seconds:
            assessment["matched_rules"] = ["telemetry_stale"]
            assessment["reason"] = (
                "telemetry is stale; current task state cannot be assessed"
            )
            return assessment

        if sample["task_state"] != "running":
            assessment["reason"] = (
                "checkpoint recommendation requires a currently running task"
            )
            return assessment

        high_water = sample["stack_high_water_mark_words"]
        assessment["risk_level"] = "normal"
        assessment["reason"] = "no configured operational-risk rule matched"
        if high_water <= self.minimum_stack_high_water_words:
            assessment["risk_level"] = "elevated"
            assessment["recommendation"] = "checkpoint_recommended"
            assessment["matched_rules"] = [
                "stack_high_water_at_or_below_configured_minimum"
            ]
            assessment["reason"] = (
                "measured unused stack reserve is at or below the configured threshold"
            )
        return assessment
