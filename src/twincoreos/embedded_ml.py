"""Embedded telemetry collection, anomaly inference, and shadow evaluation."""

from __future__ import annotations

import argparse
from collections import defaultdict, deque
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
import pickle
from pathlib import Path
import tempfile
import threading
from typing import Any, Iterable, Mapping, Sequence
import uuid

import joblib
from sklearn.ensemble import IsolationForest
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
import sklearn

from twincoreos.embedded_risk import EMBEDDED_CHECKPOINT_TASK_IDS, EMBEDDED_TASK_IDS
from twincoreos.embedded_telemetry import EmbeddedTelemetryError, validate_task_telemetry


LOGGER = logging.getLogger(__name__)
DATASET_SCHEMA_VERSION = 1
FEATURE_SCHEMA_VERSION = 1
MODEL_ARTIFACT_SCHEMA_VERSION = 1
MODEL_TYPE = "embedded-isolation-forest"
DEFAULT_EMBEDDED_MODEL_PATH = Path(
    ".twincoreos/models/embedded-isolation-forest.joblib"
)
DEFAULT_EMBEDDED_DATA_DIR = Path(".twincoreos/embedded-data")
FEATURE_NAMES = (
    "task_sensor_sampler",
    "task_control_loop",
    "state_running",
    "state_ready",
    "state_blocked",
    "state_suspended",
    "state_deleted",
    "stack_high_water_mark_words",
    "stack_reserve_delta_words",
    "execution_delta",
    "tick_delta",
    "time_since_previous_seconds",
    "telemetry_age_seconds",
    "has_temporal_context",
    "telemetry_fresh",
)
SCENARIO_TYPES = (
    "normal_operation",
    "gradual_stack_reduction",
    "sudden_stack_reduction",
    "stalled_progress",
    "recovery_progress",
)
_TASK_STATES = ("running", "ready", "blocked", "suspended", "deleted")


class EmbeddedMLDataError(ValueError):
    """An embedded ML dataset or feature record is invalid."""


class EmbeddedModelError(RuntimeError):
    """An embedded anomaly-model artifact is missing or incompatible."""


def _utc_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise EmbeddedMLDataError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise EmbeddedMLDataError(f"{field} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise EmbeddedMLDataError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _timestamp(value: datetime | None = None) -> str:
    current = datetime.now(timezone.utc) if value is None else value
    if current.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return current.astimezone(timezone.utc).isoformat()


def _counter_delta(current: int, previous: int) -> int:
    return (current - previous) & 0xFFFFFFFF


def _valid_number(value: object, field: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
    ):
        raise EmbeddedMLDataError(f"feature {field} must be a finite number")
    return float(value)


class EmbeddedFeatureHistory:
    """Extract versioned telemetry-only features with per-session task context."""

    def __init__(self, *, stale_after_seconds: float = 3.0) -> None:
        if not math.isfinite(stale_after_seconds) or stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be finite and positive")
        self.stale_after_seconds = float(stale_after_seconds)
        self._previous: dict[tuple[str, str], dict[str, Any]] = {}

    def reset(
        self,
        *,
        bridge_session_id: str | None = None,
        task_id: str | None = None,
    ) -> None:
        if bridge_session_id is None and task_id is None:
            self._previous.clear()
            return
        self._previous = {
            key: sample
            for key, sample in self._previous.items()
            if not (
                (bridge_session_id is None or key[0] == bridge_session_id)
                and (task_id is None or key[1] == task_id)
            )
        }

    def extract(
        self,
        telemetry: Mapping[str, Any],
        *,
        bridge_session_id: str,
        observed_at_utc: str,
        telemetry_age_seconds: float,
        telemetry_fresh: bool,
    ) -> dict[str, float]:
        sample = validate_task_telemetry(dict(telemetry))
        if sample["task_id"] not in EMBEDDED_TASK_IDS:
            raise EmbeddedMLDataError("unsupported embedded task identity")
        if not isinstance(bridge_session_id, str) or not bridge_session_id:
            raise EmbeddedMLDataError("bridge_session_id is required")
        observed_at = _utc_datetime(observed_at_utc, "observed_at_utc")
        age = _valid_number(telemetry_age_seconds, "telemetry_age_seconds")
        if age < 0:
            raise EmbeddedMLDataError("telemetry_age_seconds cannot be negative")

        key = (bridge_session_id, sample["task_id"])
        previous = self._previous.get(key)
        sample_time = observed_at.timestamp()
        has_previous = previous is not None
        if previous is not None:
            elapsed = sample_time - previous["observed_at"]
            if (
                elapsed <= 0
                or elapsed > self.stale_after_seconds
                or previous["run_id"] != sample.get("run_id")
            ):
                previous = None
                has_previous = False

        features: dict[str, float] = {
            "task_sensor_sampler": float(sample["task_id"] == "sensor-sampler"),
            "task_control_loop": float(sample["task_id"] == "control-loop"),
            **{
                f"state_{state}": float(sample["task_state"] == state)
                for state in _TASK_STATES
            },
            "stack_high_water_mark_words": float(
                sample["stack_high_water_mark_words"]
            ),
            "stack_reserve_delta_words": 0.0,
            "execution_delta": 0.0,
            "tick_delta": 0.0,
            "time_since_previous_seconds": 0.0,
            "telemetry_age_seconds": age,
            "has_temporal_context": float(has_previous),
            "telemetry_fresh": float(telemetry_fresh),
        }
        if previous is not None:
            features.update(
                {
                    "stack_reserve_delta_words": float(
                        sample["stack_high_water_mark_words"]
                        - previous["stack_reserve"]
                    ),
                    "execution_delta": float(
                        _counter_delta(
                            sample["execution_count"], previous["execution_count"]
                        )
                    ),
                    "tick_delta": float(
                        _counter_delta(sample["tick_count"], previous["tick_count"])
                    ),
                    "time_since_previous_seconds": sample_time
                    - previous["observed_at"],
                }
            )
        if tuple(features) != FEATURE_NAMES:
            raise RuntimeError("embedded feature order does not match schema")
        self._previous[key] = {
            "observed_at": sample_time,
            "run_id": sample.get("run_id"),
            "stack_reserve": sample["stack_high_water_mark_words"],
            "execution_count": sample["execution_count"],
            "tick_count": sample["tick_count"],
        }
        return features


def validate_dataset_record(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EmbeddedMLDataError("dataset record must be an object")
    required = {
        "dataset_schema_version",
        "feature_schema_version",
        "observed_at_utc",
        "task_id",
        "bridge_session_id",
        "source_group_id",
        "data_source",
        "telemetry_valid",
        "telemetry_fresh",
        "training_eligible",
        "raw_telemetry",
        "features",
    }
    if not required <= value.keys():
        raise EmbeddedMLDataError(
            "dataset record is missing fields: "
            + ", ".join(sorted(required - value.keys()))
        )
    if value["dataset_schema_version"] != DATASET_SCHEMA_VERSION:
        raise EmbeddedMLDataError("unsupported embedded dataset schema version")
    if value["feature_schema_version"] != FEATURE_SCHEMA_VERSION:
        raise EmbeddedMLDataError("unsupported embedded feature schema version")
    _utc_datetime(value["observed_at_utc"], "observed_at_utc")
    if value["task_id"] not in EMBEDDED_TASK_IDS:
        raise EmbeddedMLDataError("unsupported embedded dataset task identity")
    for field in ("bridge_session_id", "source_group_id"):
        if not isinstance(value[field], str) or not value[field]:
            raise EmbeddedMLDataError(f"{field} must be a non-empty string")
    if value["source_group_id"] != value["bridge_session_id"]:
        raise EmbeddedMLDataError(
            "source_group_id must identify the originating bridge session"
        )
    if value["data_source"] not in {"live_qemu_uart", "simulated_scenario"}:
        raise EmbeddedMLDataError("unsupported embedded dataset data_source")
    for field in ("telemetry_valid", "telemetry_fresh", "training_eligible"):
        if not isinstance(value[field], bool):
            raise EmbeddedMLDataError(f"{field} must be boolean")
    if value["telemetry_valid"] is not True:
        raise EmbeddedMLDataError("invalid telemetry cannot be used as a model record")
    raw = value["raw_telemetry"]
    if not isinstance(raw, dict):
        raise EmbeddedMLDataError("raw_telemetry must be an object")
    try:
        checked = validate_task_telemetry(raw)
    except EmbeddedTelemetryError as error:
        raise EmbeddedMLDataError(f"raw embedded telemetry is invalid: {error}") from error
    if checked["task_id"] != value["task_id"]:
        raise EmbeddedMLDataError("dataset task_id does not match raw telemetry")
    if value["data_source"] == "live_qemu_uart" and value["training_eligible"]:
        curation = value.get("curation")
        if (
            not isinstance(curation, dict)
            or curation.get("method") != "explicit_operator_baseline_selection"
            or curation.get("labels_used_for_training") is not False
            or not isinstance(curation.get("minimum_stack_high_water_words"), int)
            or isinstance(curation.get("minimum_stack_high_water_words"), bool)
        ):
            raise EmbeddedMLDataError(
                "training-eligible live data requires explicit baseline curation metadata"
            )
        _utc_datetime(curation.get("selected_at_utc"), "curation.selected_at_utc")
        if (
            value["telemetry_fresh"] is not True
            or checked["task_state"] != "running"
            or checked["stack_high_water_mark_words"]
            <= curation["minimum_stack_high_water_words"]
            or checked.get("run_id")
        ):
            raise EmbeddedMLDataError(
                "training-eligible records must be fresh, running, above-threshold, and pre-fault"
            )
    features = value["features"]
    if not isinstance(features, dict) or set(features) != set(FEATURE_NAMES):
        raise EmbeddedMLDataError(
            "features do not match embedded feature schema v1"
        )
    for name in FEATURE_NAMES:
        _valid_number(features[name], name)
    if value["data_source"] == "simulated_scenario":
        if not all(
            isinstance(value.get(field), str) and value[field]
            for field in ("scenario_id", "scenario_type", "reference_basis")
        ):
            raise EmbeddedMLDataError(
                "simulated records require scenario identity and reference basis"
            )
        if value.get("scenario_type") not in SCENARIO_TYPES:
            raise EmbeddedMLDataError("unsupported simulated scenario type")
        if not isinstance(value.get("reference_condition"), bool):
            raise EmbeddedMLDataError(
                "simulated records require an explicit reference condition"
            )
        if value.get("training_eligible") is not False:
            raise EmbeddedMLDataError(
                "simulated scenarios cannot be marked baseline training data"
            )
    elif value.get("training_eligible") and value.get("scenario_id") is not None:
        raise EmbeddedMLDataError(
            "simulated scenario labels cannot be used as training labels"
        )
    return value


def read_embedded_dataset(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    records: list[dict[str, Any]] = []
    with source.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise EmbeddedMLDataError(
                    f"{source}:{line_number}: invalid JSON: {error.msg}"
                ) from error
            try:
                records.append(validate_dataset_record(record))
            except EmbeddedMLDataError as error:
                raise EmbeddedMLDataError(
                    f"{source}:{line_number}: {error}"
                ) from error
    if not records:
        raise EmbeddedMLDataError(f"{source} contains no dataset records")
    return records


def _write_jsonl_new(path: str | Path, records: Iterable[Mapping[str, Any]]) -> int:
    destination = Path(path)
    validated_records = [
        validate_dataset_record(dict(record)) for record in records
    ]
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8") as handle:
        for record in validated_records:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")))
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return len(validated_records)


class EmbeddedTelemetryJournal:
    """Append validated real QEMU samples to a separate JSONL dataset."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, record: Mapping[str, Any]) -> None:
        validated = validate_dataset_record(dict(record))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(validated, sort_keys=True, separators=(",", ":"))
            )
            handle.write("\n")
            handle.flush()


def make_live_dataset_record(
    telemetry: Mapping[str, Any],
    *,
    bridge_session_id: str,
    observed_at_utc: str,
    telemetry_age_seconds: float,
    telemetry_fresh: bool,
    features: Mapping[str, float],
) -> dict[str, Any]:
    record = {
        "dataset_schema_version": DATASET_SCHEMA_VERSION,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "observed_at_utc": observed_at_utc,
        "task_id": telemetry["task_id"],
        "bridge_session_id": bridge_session_id,
        "source_group_id": bridge_session_id,
        "run_id": telemetry.get("run_id"),
        "data_source": "live_qemu_uart",
        "telemetry_valid": True,
        "telemetry_fresh": telemetry_fresh,
        "training_eligible": False,
        "raw_telemetry": dict(telemetry),
        "features": dict(features),
        "reference_condition": None,
        "reference_basis": None,
    }
    return validate_dataset_record(record)


def curate_baseline_session(
    records: Sequence[Mapping[str, Any]],
    *,
    bridge_session_id: str,
    minimum_stack_high_water_words: int,
) -> list[dict[str, Any]]:
    if minimum_stack_high_water_words < 0:
        raise ValueError("minimum stack threshold cannot be negative")
    selected: list[dict[str, Any]] = []
    for original in records:
        record = validate_dataset_record(dict(original))
        raw = record["raw_telemetry"]
        if (
            record["data_source"] == "live_qemu_uart"
            and record["bridge_session_id"] == bridge_session_id
            and record["telemetry_fresh"]
            and record["task_id"] in EMBEDDED_TASK_IDS
            and raw["task_state"] == "running"
            and raw["stack_high_water_mark_words"]
            > minimum_stack_high_water_words
            and not raw.get("run_id")
        ):
            curated = dict(record)
            curated["training_eligible"] = True
            curated["curation"] = {
                "method": "explicit_operator_baseline_selection",
                "selected_at_utc": _timestamp(),
                "minimum_stack_high_water_words": minimum_stack_high_water_words,
                "labels_used_for_training": False,
            }
            selected.append(curated)
    if not selected:
        raise EmbeddedMLDataError(
            "no fresh, running, above-threshold, pre-fault telemetry matched the selected session"
        )
    if {record["task_id"] for record in selected} != set(EMBEDDED_TASK_IDS):
        raise EmbeddedMLDataError(
            "selected baseline session must contain both embedded task identities"
        )
    return selected


def generate_simulated_scenarios(
    records: Sequence[Mapping[str, Any]],
    *,
    minimum_stack_high_water_words: int,
) -> list[dict[str, Any]]:
    if minimum_stack_high_water_words < 0:
        raise ValueError("minimum stack threshold cannot be negative")
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for original in records:
        record = validate_dataset_record(dict(original))
        if record["data_source"] != "live_qemu_uart":
            raise EmbeddedMLDataError(
                "scenario generation input must contain recorded live QEMU telemetry"
            )
        if record["telemetry_fresh"]:
            by_task[record["task_id"]].append(record)
    if set(by_task) != set(EMBEDDED_TASK_IDS):
        raise EmbeddedMLDataError(
            "scenario source must contain fresh live data for both embedded tasks"
        )
    generated: list[dict[str, Any]] = []
    for task_id, task_records in by_task.items():
        task_records.sort(key=lambda item: item["observed_at_utc"])
        if len(task_records) < 3:
            raise EmbeddedMLDataError(
                f"scenario generation requires at least three fresh samples for {task_id}"
            )
        source_groups = {record["source_group_id"] for record in task_records}
        if len(source_groups) != 1:
            raise EmbeddedMLDataError(
                "generate one scenario dataset per independent QEMU bridge session"
            )
        source_group_id = next(iter(source_groups))
        scenario_defs: dict[str, list[dict[str, Any]]] = {}
        for scenario_type in SCENARIO_TYPES:
            scenario_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"twincoreos:{source_group_id}:{task_id}:{scenario_type}",
                )
            )
            scenario_defs[scenario_type] = []
            initial_reserve = task_records[0]["raw_telemetry"][
                "stack_high_water_mark_words"
            ]
            recovery_index = max(1, len(task_records) // 2)
            for index, original in enumerate(task_records):
                record = dict(original)
                raw = dict(original["raw_telemetry"])
                reserve = raw["stack_high_water_mark_words"]
                reference_condition = False
                basis = "unchanged copy of recorded telemetry; no failure label"
                if scenario_type == "gradual_stack_reduction":
                    target_reserve = max(0, minimum_stack_high_water_words - 1)
                    reserve = round(
                        initial_reserve
                        + (target_reserve - initial_reserve)
                        * index
                        / (len(task_records) - 1)
                    )
                    raw["stack_high_water_mark_words"] = reserve
                    reference_condition = reserve <= minimum_stack_high_water_words
                    basis = "simulated gradual stack-reserve transform; condition is configured threshold crossing"
                elif scenario_type == "sudden_stack_reduction":
                    if index >= recovery_index:
                        reserve = min(
                            reserve,
                            max(0, minimum_stack_high_water_words - 1),
                        )
                        raw["stack_high_water_mark_words"] = reserve
                        reference_condition = True
                    basis = "simulated sudden stack-reserve transform; condition is configured threshold crossing"
                elif scenario_type == "stalled_progress":
                    if index >= recovery_index:
                        raw["execution_count"] = task_records[
                            recovery_index
                        ]["raw_telemetry"]["execution_count"]
                        raw["tick_count"] = task_records[
                            recovery_index
                        ]["raw_telemetry"]["tick_count"]
                        reference_condition = True
                    basis = "simulated repeated counters; operating-condition label is not a task-failure label"
                elif scenario_type == "recovery_progress":
                    if index < recovery_index:
                        raw["stack_high_water_mark_words"] = min(
                            reserve,
                            max(0, minimum_stack_high_water_words - 1),
                        )
                        reference_condition = True
                    else:
                        raw["stack_high_water_mark_words"] = max(
                            reserve, minimum_stack_high_water_words + 1
                        )
                    basis = "simulated low-reserve then recovery sequence; labels identify the transformed risky interval only"
                raw["run_id"] = scenario_id
                scenario_defs[scenario_type].append(
                    {
                        "dataset_schema_version": DATASET_SCHEMA_VERSION,
                        "feature_schema_version": FEATURE_SCHEMA_VERSION,
                        "observed_at_utc": original["observed_at_utc"],
                        "task_id": task_id,
                        "bridge_session_id": original["bridge_session_id"],
                        "source_group_id": source_group_id,
                        "run_id": scenario_id,
                        "data_source": "simulated_scenario",
                        "telemetry_valid": True,
                        "telemetry_fresh": True,
                        "training_eligible": False,
                        "raw_telemetry": raw,
                        "features": {},
                        "scenario_id": scenario_id,
                        "scenario_type": scenario_type,
                        "reference_condition": reference_condition,
                        "reference_basis": basis,
                    }
                )
            feature_history = EmbeddedFeatureHistory()
            for simulated in scenario_defs[scenario_type]:
                simulated["features"] = feature_history.extract(
                    simulated["raw_telemetry"],
                    bridge_session_id=simulated["bridge_session_id"],
                    observed_at_utc=simulated["observed_at_utc"],
                    telemetry_age_seconds=0.0,
                    telemetry_fresh=True,
                )
                generated.append(validate_dataset_record(simulated))
    return generated


def _dataset_digest(records: Sequence[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for record in records:
        digest.update(
            json.dumps(record["features"], sort_keys=True, separators=(",", ":")).encode()
        )
        digest.update(b"\n")
    return digest.hexdigest()


def train_embedded_model(
    records: Sequence[Mapping[str, Any]],
    output_path: str | Path,
    *,
    contamination: float = 0.05,
    random_state: int = 42,
    overwrite: bool = False,
) -> dict[str, Any]:
    if not 0 < contamination <= 0.5:
        raise ValueError("contamination must be in (0, 0.5]")
    curated = [validate_dataset_record(dict(record)) for record in records]
    training = [
        record
        for record in curated
        if record["data_source"] == "live_qemu_uart"
        and record["training_eligible"] is True
        and record["telemetry_fresh"] is True
    ]
    if len(training) < 30:
        raise EmbeddedMLDataError(
            f"at least 30 explicitly curated fresh baseline observations are required; found {len(training)}"
        )
    if {record["task_id"] for record in training} != set(EMBEDDED_TASK_IDS):
        raise EmbeddedMLDataError(
            "curated training data must include both embedded task identities"
        )
    source_groups = sorted({record["source_group_id"] for record in training})
    if len(source_groups) < 2:
        raise EmbeddedMLDataError(
            "training data must span at least two independently collected QEMU sessions"
        )
    vectors = [
        [record["features"][name] for name in FEATURE_NAMES]
        for record in training
    ]
    model = Pipeline(
        [
            ("scaler", StandardScaler()),
            (
                "isolation_forest",
                IsolationForest(
                    contamination=contamination,
                    random_state=random_state,
                    n_estimators=200,
                ),
            ),
        ]
    )
    model.fit(vectors)
    dataset_sha256 = _dataset_digest(training)
    model_version = f"embedded-iforest-v{MODEL_ARTIFACT_SCHEMA_VERSION}-{dataset_sha256[:12]}"
    artifact = {
        "artifact_schema_version": MODEL_ARTIFACT_SCHEMA_VERSION,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_names": FEATURE_NAMES,
        "model_type": MODEL_TYPE,
        "model_version": model_version,
        "trained_at_utc": _timestamp(),
        "sklearn_version": sklearn.__version__,
        "model_configuration": {
            "contamination": contamination,
            "random_state": random_state,
            "n_estimators": 200,
            "preprocessing": ["StandardScaler"],
            "training_method": "unsupervised_isolation_forest",
            "labels_used": False,
        },
        "training_manifest": {
            "dataset_sha256": dataset_sha256,
            "record_count": len(training),
            "source_groups": source_groups,
            "task_ids": sorted({record["task_id"] for record in training}),
            "data_sources": sorted({record["data_source"] for record in training}),
        },
        "pipeline": model,
    }
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not overwrite:
        raise FileExistsError(
            f"model artifact already exists: {destination}; pass --overwrite to replace it"
        )
    with tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        joblib.dump(artifact, temporary_path, compress=3)
        if overwrite:
            os.replace(temporary_path, destination)
        else:
            os.link(temporary_path, destination)
            temporary_path.unlink()
    finally:
        temporary_path.unlink(missing_ok=True)
    return {
        "success": True,
        "model_version": model_version,
        "output_path": str(destination),
        "record_count": len(training),
        "source_groups": source_groups,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "dataset_sha256": dataset_sha256,
        "model_configuration": artifact["model_configuration"],
    }


def load_embedded_model(path: str | Path) -> dict[str, Any]:
    artifact_path = Path(path)
    if not artifact_path.is_file():
        raise EmbeddedModelError(f"model artifact not found: {artifact_path}")
    try:
        artifact = joblib.load(artifact_path)
    except (
        AttributeError,
        EOFError,
        ImportError,
        KeyError,
        ModuleNotFoundError,
        OSError,
        pickle.UnpicklingError,
        TypeError,
        ValueError,
    ) as error:
        raise EmbeddedModelError(
            f"could not load embedded model artifact: {error}"
        ) from error
    if not isinstance(artifact, dict):
        raise EmbeddedModelError("embedded model artifact must be a mapping")
    if artifact.get("artifact_schema_version") != MODEL_ARTIFACT_SCHEMA_VERSION:
        raise EmbeddedModelError("incompatible embedded model artifact version")
    if artifact.get("feature_schema_version") != FEATURE_SCHEMA_VERSION:
        raise EmbeddedModelError("incompatible embedded feature schema version")
    if tuple(artifact.get("feature_names", ())) != FEATURE_NAMES:
        raise EmbeddedModelError("embedded model feature names do not match schema")
    if artifact.get("model_type") != MODEL_TYPE:
        raise EmbeddedModelError("embedded model type is not supported")
    trained_sklearn = artifact.get("sklearn_version")
    if not isinstance(trained_sklearn, str) or trained_sklearn.split(".")[:2] != sklearn.__version__.split(".")[:2]:
        raise EmbeddedModelError(
            f"model scikit-learn version {trained_sklearn!r} is incompatible with {sklearn.__version__}"
        )
    pipeline = artifact.get("pipeline")
    if not callable(getattr(pipeline, "decision_function", None)) or not callable(
        getattr(pipeline, "predict", None)
    ):
        raise EmbeddedModelError("embedded model pipeline is missing inference methods")
    manifest = artifact.get("training_manifest")
    if not isinstance(manifest, dict) or not isinstance(
        manifest.get("source_groups"), list
    ):
        raise EmbeddedModelError("embedded model training manifest is invalid")
    return artifact


def infer_embedded_anomaly(
    artifact: Mapping[str, Any],
    *,
    task_id: str,
    observed_at_utc: str,
    telemetry_fresh: bool,
    features: Mapping[str, float],
) -> dict[str, Any]:
    if task_id not in EMBEDDED_TASK_IDS:
        raise EmbeddedMLDataError("unsupported embedded task identity")
    result: dict[str, Any] = {
        "status": "unavailable",
        "task_id": task_id,
        "observed_at_utc": observed_at_utc,
        "telemetry_fresh": telemetry_fresh,
        "model_type": artifact["model_type"],
        "model_version": artifact["model_version"],
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_summary": {
            name: features[name]
            for name in (
                "stack_high_water_mark_words",
                "stack_reserve_delta_words",
                "execution_delta",
                "tick_delta",
                "time_since_previous_seconds",
                "telemetry_age_seconds",
            )
        },
        "limitations": (
            "Isolation Forest anomaly output is not a calibrated probability of task failure."
        ),
    }
    if not telemetry_fresh:
        result["reason"] = "stale or invalid telemetry is not evaluated"
        return result
    vector = [[_valid_number(features[name], name) for name in FEATURE_NAMES]]
    pipeline = artifact["pipeline"]
    decision_value = float(pipeline.decision_function(vector)[0])
    prediction = int(pipeline.predict(vector)[0])
    if not math.isfinite(decision_value) or prediction not in {-1, 1}:
        raise EmbeddedModelError("model returned an invalid anomaly result")
    result.update(
        {
            "status": "available",
            "prediction": "anomaly" if prediction == -1 else "inlier",
            "anomaly": prediction == -1,
            "raw_decision_function": decision_value,
            "raw_anomaly_score": -decision_value,
            "reason": (
                "model classified this feature vector as an anomaly"
                if prediction == -1
                else "model classified this feature vector as an inlier"
            ),
        }
    )
    return result


class EmbeddedMLShadowMode:
    """Evaluate embedded anomaly output without issuing checkpoint commands."""

    def __init__(
        self,
        *,
        model_path: str | Path = DEFAULT_EMBEDDED_MODEL_PATH,
        data_dir: str | Path = DEFAULT_EMBEDDED_DATA_DIR,
        stale_after_seconds: float = 3.0,
    ) -> None:
        self.model_path = Path(model_path)
        self.data_dir = Path(data_dir)
        self._journal = EmbeddedTelemetryJournal(
            self.data_dir / "live-observations.jsonl"
        )
        self._decision_path = self.data_dir / "shadow-decisions.jsonl"
        self._features = EmbeddedFeatureHistory(
            stale_after_seconds=stale_after_seconds
        )
        self._latest: dict[str, dict[str, Any]] = {}
        self._decisions: deque[dict[str, Any]] = deque(maxlen=100)
        self._journal_error: str | None = None
        self._decision_error: str | None = None
        self._lock = threading.RLock()
        self._artifact: dict[str, Any] | None = None
        self._model_load_error: str | None = None
        try:
            self._artifact = load_embedded_model(self.model_path)
            LOGGER.info(
                "Embedded ML shadow mode loaded %s (%s)",
                self._artifact["model_version"],
                self.model_path,
            )
        except EmbeddedModelError as error:
            self._model_load_error = str(error)
            LOGGER.warning(
                "Embedded ML inference unavailable; threshold policy remains active: %s",
                error,
            )

    def reset_session(self) -> None:
        with self._lock:
            self._features.reset()
            self._latest.clear()

    def observe_event(self, bridge_session_id: str, event: Mapping[str, Any]) -> None:
        if event.get("event") == "task_restarted":
            with self._lock:
                self._features.reset(
                    bridge_session_id=bridge_session_id,
                    task_id=str(event.get("task_id", "")),
                )

    def ingest(
        self,
        telemetry: Mapping[str, Any],
        *,
        bridge_session_id: str,
        observed_at_utc: str,
        assessment: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._lock:
            task_id = str(telemetry["task_id"])
            fresh = (
                assessment.get("telemetry_freshness") == "fresh"
                and assessment.get("risk_category") != "UNKNOWN"
            )
            observed_inputs = assessment.get("observed_inputs", {})
            age = observed_inputs.get("telemetry_age_seconds", 0.0)
            features = self._features.extract(
                telemetry,
                bridge_session_id=bridge_session_id,
                observed_at_utc=observed_at_utc,
                telemetry_age_seconds=float(age),
                telemetry_fresh=fresh,
            )
            record = make_live_dataset_record(
                telemetry,
                bridge_session_id=bridge_session_id,
                observed_at_utc=observed_at_utc,
                telemetry_age_seconds=float(age),
                telemetry_fresh=fresh,
                features=features,
            )
            try:
                self._journal.append(record)
                self._journal_error = None
            except (OSError, EmbeddedMLDataError) as error:
                self._journal_error = str(error)
                LOGGER.exception("Could not append embedded telemetry dataset record")

            if self._artifact is None:
                ml_result = {
                    "status": "unavailable",
                    "task_id": task_id,
                    "observed_at_utc": observed_at_utc,
                    "telemetry_fresh": fresh,
                    "model_type": MODEL_TYPE,
                    "model_version": None,
                    "feature_schema_version": FEATURE_SCHEMA_VERSION,
                    "feature_summary": {
                        name: features[name]
                        for name in (
                            "stack_high_water_mark_words",
                            "stack_reserve_delta_words",
                            "execution_delta",
                            "tick_delta",
                            "time_since_previous_seconds",
                            "telemetry_age_seconds",
                        )
                    },
                    "reason": "embedded model is not loaded",
                    "limitations": "No ML result is available; this is not LOW risk.",
                }
            else:
                try:
                    ml_result = infer_embedded_anomaly(
                        self._artifact,
                        task_id=task_id,
                        observed_at_utc=observed_at_utc,
                        telemetry_fresh=fresh,
                        features=features,
                    )
                except (
                    AttributeError,
                    EmbeddedMLDataError,
                    EmbeddedModelError,
                    IndexError,
                    TypeError,
                    ValueError,
                ) as error:
                    LOGGER.exception("Embedded ML inference failed for %s", task_id)
                    ml_result = {
                        "status": "unavailable",
                        "task_id": task_id,
                        "observed_at_utc": observed_at_utc,
                        "telemetry_fresh": fresh,
                        "model_type": MODEL_TYPE,
                        "model_version": self._artifact.get("model_version"),
                        "feature_schema_version": FEATURE_SCHEMA_VERSION,
                        "feature_summary": {},
                        "reason": f"inference failed: {error}",
                        "limitations": "No ML result is available; this is not LOW risk.",
                    }

            threshold_recommendation = assessment.get("recommendation", "none")
            proposed_decision, reason, difference = self._proposed_decision(
                task_id,
                assessment,
                ml_result,
            )
            decision = {
                "occurred_at_utc": observed_at_utc,
                "task_id": task_id,
                "bridge_session_id": bridge_session_id,
                "run_id": telemetry.get("run_id"),
                "model_version": ml_result.get("model_version"),
                "mode": "shadow",
                "features": features,
                "threshold_policy": {
                    "category": assessment.get("risk_category"),
                    "risk_index": assessment.get("risk_index"),
                    "recommendation": threshold_recommendation,
                    "reason": assessment.get("reason"),
                },
                "ml_assessment": ml_result,
                "proposed_ml_decision": proposed_decision,
                "reason": reason,
                "difference_reason": difference,
                "checkpoint_command_issued": False,
            }
            self._decisions.append(decision)
            try:
                self._decision_path.parent.mkdir(parents=True, exist_ok=True)
                with self._decision_path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            decision, sort_keys=True, separators=(",", ":")
                        )
                    )
                    handle.write("\n")
                    handle.flush()
                self._decision_error = None
            except OSError as error:
                self._decision_error = str(error)
                LOGGER.exception("Could not persist embedded ML shadow decision")
            self._latest[task_id] = decision
            return decision

    @staticmethod
    def _proposed_decision(
        task_id: str,
        assessment: Mapping[str, Any],
        ml_result: Mapping[str, Any],
    ) -> tuple[str, str, str | None]:
        threshold = assessment.get("recommendation", "none")
        if assessment.get("telemetry_freshness") != "fresh":
            return (
                "unavailable",
                "fresh embedded telemetry is required for a shadow proposal",
                "threshold assessment is UNKNOWN; ML output cannot substitute for missing data",
            )
        if task_id not in EMBEDDED_CHECKPOINT_TASK_IDS:
            if ml_result.get("anomaly") is True:
                return (
                    "checkpoint_unsupported",
                    "anomaly observed, but checkpointing is not implemented for this task",
                    "ML does not enable unsupported checkpointing",
                )
            return (
                "none",
                "no checkpoint action is supported or proposed for this task",
                None,
            )
        if threshold == "checkpoint_recommended":
            difference = (
                "hard threshold recommendation remains authoritative"
                if ml_result.get("anomaly") is not True
                else None
            )
            return (
                "checkpoint_recommended",
                "existing hard-threshold policy recommends a checkpoint; shadow mode does not dispatch it",
                difference,
            )
        if ml_result.get("status") != "available":
            return (
                "unavailable",
                "ML result is unavailable; existing threshold policy remains unchanged",
                "no ML inference is interpreted as LOW risk or as a checkpoint decision",
            )
        if ml_result.get("anomaly") is True:
            return (
                "checkpoint_candidate",
                "ML anomaly would be an early checkpoint candidate in shadow mode only",
                "ML candidate differs from the threshold policy's no-checkpoint result",
            )
        return (
            "none",
            "threshold policy and ML shadow output do not propose an early checkpoint",
            None,
        )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            artifact = self._artifact
            return {
                "mode": "shadow",
                "shadow_only": True,
                "checkpoint_commands_enabled": False,
                "model": {
                    "available": artifact is not None,
                    "path": str(self.model_path),
                    "model_type": MODEL_TYPE,
                    "model_version": (
                        artifact.get("model_version") if artifact else None
                    ),
                    "artifact_schema_version": MODEL_ARTIFACT_SCHEMA_VERSION,
                    "feature_schema_version": FEATURE_SCHEMA_VERSION,
                    "sklearn_version": (
                        artifact.get("sklearn_version") if artifact else None
                    ),
                    "reason": (
                        "model loaded"
                        if artifact
                        else self._model_load_error
                    ),
                },
                "latest_by_task": [
                    self._latest[task_id]
                    for task_id in sorted(self._latest)
                ],
                "recent_decisions": list(self._decisions),
                "dataset": {
                    "live_observations_path": str(self._journal.path),
                    "shadow_decisions_path": str(self._decision_path),
                    "live_journal_error": self._journal_error,
                    "shadow_log_error": self._decision_error,
                    "automatic_training": False,
                },
            }


def _confusion_metrics(
    expected: Sequence[bool], predicted: Sequence[bool]
) -> dict[str, Any]:
    tp = sum(actual and guess for actual, guess in zip(expected, predicted, strict=True))
    tn = sum(not actual and not guess for actual, guess in zip(expected, predicted, strict=True))
    fp = sum(not actual and guess for actual, guess in zip(expected, predicted, strict=True))
    fn = sum(actual and not guess for actual, guess in zip(expected, predicted, strict=True))
    positive_denominator = tp + fn
    negative_denominator = tn + fp
    return {
        "sample_count": len(expected),
        "confusion_matrix": {
            "true_positive": tp,
            "false_positive": fp,
            "true_negative": tn,
            "false_negative": fn,
        },
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / positive_denominator if positive_denominator else None,
        "false_positive_rate": fp / negative_denominator if negative_denominator else None,
        "false_negative_rate": fn / positive_denominator if positive_denominator else None,
    }


def evaluate_embedded_policy(
    records: Sequence[Mapping[str, Any]],
    artifact: Mapping[str, Any],
    *,
    minimum_stack_high_water_words: int,
) -> dict[str, Any]:
    checked = [validate_dataset_record(dict(record)) for record in records]
    groups = {record["source_group_id"] for record in checked}
    training_groups = set(artifact["training_manifest"]["source_groups"])
    overlap = sorted(groups & training_groups)
    if overlap:
        raise EmbeddedMLDataError(
            "held-out evaluation source groups overlap model training: "
            + ", ".join(overlap)
        )
    labeled = [
        record
        for record in checked
        if record["data_source"] == "simulated_scenario"
        and isinstance(record.get("reference_condition"), bool)
    ]
    if not labeled:
        return {
            "metrics_supported": False,
            "reason": "no independently labeled simulated scenario records are available",
            "source_groups": sorted(groups),
            "training_source_groups": sorted(training_groups),
            "labels_are_failure_labels": False,
        }
    expected: list[bool] = []
    threshold_predictions: list[bool] = []
    ml_predictions: list[bool] = []
    ml_available = True
    proposals: dict[tuple[str, str], list[bool]] = defaultdict(list)
    timeline: dict[tuple[str, str], list[tuple[datetime, bool, bool]]] = defaultdict(list)
    for record in labeled:
        raw = record["raw_telemetry"]
        expected.append(record["reference_condition"])
        threshold_flag = (
            record["telemetry_fresh"]
            and raw["stack_high_water_mark_words"]
            <= minimum_stack_high_water_words
        )
        threshold_predictions.append(threshold_flag)
        result = infer_embedded_anomaly(
            artifact,
            task_id=record["task_id"],
            observed_at_utc=record["observed_at_utc"],
            telemetry_fresh=record["telemetry_fresh"],
            features=record["features"],
        )
        if result["status"] != "available":
            ml_available = False
            ml_flag = False
        else:
            ml_flag = bool(result["anomaly"])
        ml_predictions.append(ml_flag)
        key = (str(record.get("scenario_id")), record["task_id"])
        timeline[key].append(
            (
                _utc_datetime(record["observed_at_utc"], "observed_at_utc"),
                record["reference_condition"],
                ml_flag,
            )
        )
        proposal = record["task_id"] == "sensor-sampler" and (
            threshold_flag or ml_flag
        )
        proposals[key].append(proposal)

    ordered_timelines = {
        key: sorted(values, key=lambda value: value[0])
        for key, values in timeline.items()
    }
    lead_times: list[float] = []
    for values in ordered_timelines.values():
        condition_time = next(
            (timestamp for timestamp, condition, _ in values if condition), None
        )
        anomaly_time = next(
            (timestamp for timestamp, _, anomaly in values if anomaly), None
        )
        if condition_time is not None and anomaly_time is not None:
            lead_times.append((condition_time - anomaly_time).total_seconds())
    request_episodes = 0
    repeated = 0
    for flags in proposals.values():
        active = False
        for flag in flags:
            if flag and not active:
                request_episodes += 1
            elif flag and active:
                repeated += 1
            active = flag
    return {
        "metrics_supported": True,
        "model_version": artifact["model_version"],
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "source_groups": sorted(groups),
        "training_source_groups": sorted(training_groups),
        "labels_are_failure_labels": False,
        "reference_label_scope": "documented simulated operating conditions, not observed task failures",
        "threshold_policy": _confusion_metrics(expected, threshold_predictions),
        "ml_anomaly_detector": (
            _confusion_metrics(expected, ml_predictions)
            if ml_available
            else {
                "metrics_supported": False,
                "reason": "model inference unavailable for one or more held-out records",
            }
        ),
        "warning_lead_time_seconds_before_scenario_condition": {
            "count": len(lead_times),
            "values": lead_times,
            "note": "Positive values precede the scenario reference condition; these are not failure lead times.",
        },
        "checkpoint_proposals": {
            "episodes": request_episodes,
            "redundant_repeated_samples": repeated,
            "scope": "sensor-sampler shadow proposals only; no firmware command was sent",
        },
        "missing_or_stale_records": sum(
            not record["telemetry_fresh"] for record in checked
        ),
    }


def _combine_inputs(paths: Sequence[str]) -> list[dict[str, Any]]:
    if not paths:
        raise EmbeddedMLDataError("at least one --input dataset is required")
    records: list[dict[str, Any]] = []
    for path in paths:
        records.extend(read_embedded_dataset(path))
    return records


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="twincoreos-embedded-ml",
        description="Collect, train, and evaluate embedded ML in shadow-only mode.",
    )
    commands = parser.add_subparsers(dest="action", required=True)
    inspect_command = commands.add_parser(
        "inspect", help="summarize an embedded JSONL dataset"
    )
    inspect_command.add_argument("--input", required=True)
    curate_command = commands.add_parser(
        "curate-baseline",
        help="explicitly select normal, fresh live samples for unsupervised training",
    )
    curate_command.add_argument("--input", required=True)
    curate_command.add_argument("--output", required=True)
    curate_command.add_argument("--bridge-session-id", required=True)
    curate_command.add_argument("--threshold-words", type=int, required=True)
    curate_command.add_argument(
        "--confirm-normal-baseline",
        action="store_true",
        help="confirm the selected bridge session is a normal baseline, not a fault run",
    )
    scenario_command = commands.add_parser(
        "generate-scenarios",
        help="derive clearly labeled simulated scenarios from a recorded live session",
    )
    scenario_command.add_argument("--input", required=True)
    scenario_command.add_argument("--output", required=True)
    scenario_command.add_argument("--bridge-session-id", required=True)
    scenario_command.add_argument("--threshold-words", type=int, required=True)
    train_command = commands.add_parser(
        "train", help="train the separate embedded Isolation Forest artifact"
    )
    train_command.add_argument("--input", action="append", required=True)
    train_command.add_argument(
        "--output", default=str(DEFAULT_EMBEDDED_MODEL_PATH)
    )
    train_command.add_argument("--contamination", type=float, default=0.05)
    train_command.add_argument("--random-state", type=int, default=42)
    train_command.add_argument("--overwrite", action="store_true")
    evaluate_command = commands.add_parser(
        "evaluate", help="compare threshold and ML decisions on held-out scenarios"
    )
    evaluate_command.add_argument("--model", default=str(DEFAULT_EMBEDDED_MODEL_PATH))
    evaluate_command.add_argument("--validation", required=True)
    evaluate_command.add_argument("--test", required=True)
    evaluate_command.add_argument("--threshold-words", type=int, required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        if args.action == "inspect":
            records = read_embedded_dataset(args.input)
            print(
                json.dumps(
                    {
                        "record_count": len(records),
                        "sources": sorted({record["data_source"] for record in records}),
                        "tasks": sorted({record["task_id"] for record in records}),
                        "source_groups": sorted(
                            {record["source_group_id"] for record in records}
                        ),
                        "fresh_count": sum(record["telemetry_fresh"] for record in records),
                        "training_eligible_count": sum(
                            record["training_eligible"] for record in records
                        ),
                        "labeled_scenario_count": sum(
                            record["reference_condition"] is not None
                            for record in records
                        ),
                        "warning": "unlabeled live telemetry is not a ground-truth label",
                    },
                    separators=(",", ":"),
                )
            )
            return 0
        if args.action == "curate-baseline":
            if not args.confirm_normal_baseline:
                raise EmbeddedMLDataError(
                    "pass --confirm-normal-baseline only after verifying the selected session"
                )
            source_records = read_embedded_dataset(args.input)
            curated = curate_baseline_session(
                source_records,
                bridge_session_id=args.bridge_session_id,
                minimum_stack_high_water_words=args.threshold_words,
            )
            count = _write_jsonl_new(args.output, curated)
            print(
                json.dumps(
                    {
                        "success": True,
                        "output": args.output,
                        "record_count": count,
                        "source_group_id": args.bridge_session_id,
                        "labels_used_for_training": False,
                    },
                    separators=(",", ":"),
                )
            )
            return 0
        if args.action == "generate-scenarios":
            source_records = read_embedded_dataset(args.input)
            session_records = [
                record
                for record in source_records
                if record["bridge_session_id"] == args.bridge_session_id
            ]
            if not session_records:
                raise EmbeddedMLDataError(
                    f"no telemetry records found for bridge session {args.bridge_session_id}"
                )
            scenarios = generate_simulated_scenarios(
                session_records,
                minimum_stack_high_water_words=args.threshold_words,
            )
            count = _write_jsonl_new(args.output, scenarios)
            print(
                json.dumps(
                    {
                        "success": True,
                        "output": args.output,
                        "record_count": count,
                        "scenario_types": list(SCENARIO_TYPES),
                        "data_source": "simulated_scenario",
                        "failure_labels_fabricated": False,
                    },
                    separators=(",", ":"),
                )
            )
            return 0
        if args.action == "train":
            report = train_embedded_model(
                _combine_inputs(args.input),
                args.output,
                contamination=args.contamination,
                random_state=args.random_state,
                overwrite=args.overwrite,
            )
            print(json.dumps(report, separators=(",", ":")))
            return 0
        if args.action == "evaluate":
            artifact = load_embedded_model(args.model)
            validation = read_embedded_dataset(args.validation)
            test_records = read_embedded_dataset(args.test)
            validation_report = evaluate_embedded_policy(
                validation,
                artifact,
                minimum_stack_high_water_words=args.threshold_words,
            )
            test_report = evaluate_embedded_policy(
                test_records,
                artifact,
                minimum_stack_high_water_words=args.threshold_words,
            )
            if set(validation_report.get("source_groups", ())) & set(
                test_report.get("source_groups", ())
            ):
                raise EmbeddedMLDataError(
                    "validation and held-out test data must use separate source groups"
                )
            print(
                json.dumps(
                    {
                        "model_version": artifact["model_version"],
                        "threshold_words": args.threshold_words,
                        "validation": validation_report,
                        "held_out_test": test_report,
                        "limitations": [
                            "simulated scenario labels describe operating conditions, not observed failures",
                            "anomaly scores are not calibrated failure probabilities",
                            "performance does not establish real-world failure prediction",
                        ],
                    },
                    separators=(",", ":"),
                )
            )
            return 0
    except (
        EmbeddedMLDataError,
        EmbeddedModelError,
        EmbeddedTelemetryError,
        OSError,
        ValueError,
        RuntimeError,
    ) as error:
        LOGGER.error("Embedded ML command failed: %s", error)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
