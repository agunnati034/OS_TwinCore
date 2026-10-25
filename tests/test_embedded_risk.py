from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from twincoreos.embedded_risk import EmbeddedRiskPolicy


class EmbeddedRiskPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = EmbeddedRiskPolicy(
            minimum_stack_high_water_words=32,
            stale_after_seconds=3.0,
        )
        self.now = datetime(2026, 10, 25, 0, 0, 0, tzinfo=timezone.utc)

    def sample(self, **changes: object) -> dict[str, object]:
        return {
            "schema_version": 1,
            "message_type": "task_telemetry",
            "target": "freertos-qemu-rv32",
            "task_id": "sensor-sampler",
            "task_state": "running",
            "tick_count": 1500,
            "execution_count": 6,
            "stack_high_water_mark_words": 33,
            "received_at_utc": (self.now - timedelta(seconds=1)).isoformat(),
            **changes,
        }

    def test_stack_reserve_rule_recommends_at_threshold_boundary(self) -> None:
        assessment = self.policy.assess(
            "sensor-sampler",
            self.sample(stack_high_water_mark_words=32),
            assessed_at=self.now,
        )

        self.assertEqual(assessment["risk_level"], "elevated")
        self.assertEqual(assessment["recommendation"], "checkpoint_recommended")
        self.assertEqual(
            assessment["matched_rules"],
            ["stack_high_water_at_or_below_configured_minimum"],
        )
        self.assertEqual(
            assessment["observed_inputs"]["stack_high_water_mark_words"], 32
        )
        self.assertEqual(
            assessment["assessment_type"], "heuristic_operational_risk"
        )
        self.assertIn("not a trained", assessment["limitations"][0])
        self.assertNotIn("pid", assessment["observed_inputs"])
        self.assertNotIn("cpu_utilization_percent_one_core", assessment["observed_inputs"])
        self.assertNotIn("memory_rss_bytes", assessment["observed_inputs"])

    def test_stack_reserve_above_threshold_does_not_recommend_checkpoint(self) -> None:
        assessment = self.policy.assess(
            "sensor-sampler",
            self.sample(stack_high_water_mark_words=33),
            assessed_at=self.now,
        )

        self.assertEqual(assessment["risk_level"], "normal")
        self.assertEqual(assessment["recommendation"], "none")
        self.assertEqual(assessment["matched_rules"], [])

    def test_non_checkpointable_control_task_never_gets_recommendation(self) -> None:
        assessment = self.policy.assess(
            "control-loop",
            self.sample(task_id="control-loop", stack_high_water_mark_words=0),
            assessed_at=self.now,
        )

        self.assertEqual(assessment["risk_level"], "unknown")
        self.assertEqual(assessment["recommendation"], "none")
        self.assertEqual(
            assessment["reason"],
            "application-level checkpointing is not implemented for this task",
        )

    def test_missing_or_stale_telemetry_is_unknown_without_recommendation(self) -> None:
        missing = self.policy.assess(
            "sensor-sampler", None, assessed_at=self.now
        )
        stale = self.policy.assess(
            "sensor-sampler",
            self.sample(received_at_utc=(self.now - timedelta(seconds=3.1)).isoformat()),
            assessed_at=self.now,
        )

        self.assertEqual(missing["risk_level"], "unknown")
        self.assertEqual(missing["recommendation"], "none")
        self.assertEqual(stale["risk_level"], "unknown")
        self.assertEqual(stale["recommendation"], "none")
        self.assertEqual(stale["matched_rules"], ["telemetry_stale"])

    def test_invalid_task_identity_and_malformed_samples_fail_closed(self) -> None:
        invalid_identity = self.policy.assess(
            "worker-1",
            self.sample(task_id="worker-1", stack_high_water_mark_words=0),
            assessed_at=self.now,
        )
        malformed = self.policy.assess(
            "sensor-sampler",
            self.sample(stack_high_water_mark_words=True),
            assessed_at=self.now,
        )
        invalid_state = self.policy.assess(
            "sensor-sampler",
            self.sample(task_state="unknown"),
            assessed_at=self.now,
        )
        non_running = self.policy.assess(
            "sensor-sampler",
            self.sample(task_state="deleted", stack_high_water_mark_words=0),
            assessed_at=self.now,
        )
        zero_execution = self.policy.assess(
            "sensor-sampler",
            self.sample(execution_count=0),
            assessed_at=self.now,
        )
        malformed_record = self.policy.assess(
            "sensor-sampler",
            [],
            assessed_at=self.now,
        )
        mismatched = self.policy.assess(
            "sensor-sampler",
            self.sample(target="host-process"),
            assessed_at=self.now,
        )

        for assessment in (
            invalid_identity,
            malformed,
            invalid_state,
            non_running,
            zero_execution,
            malformed_record,
            mismatched,
        ):
            self.assertEqual(assessment["risk_level"], "unknown")
            self.assertEqual(assessment["recommendation"], "none")
