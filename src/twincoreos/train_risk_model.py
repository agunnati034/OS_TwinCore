"""Train the host-process Isolation Forest from collected telemetry JSONL."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from twincoreos.ml_risk_predictor import (
    DEFAULT_MODEL_PATH,
    read_jsonl_telemetry,
    telemetry_feature_rows,
    train_isolation_forest,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="twincoreos-train-risk-model",
        description=(
            "Fit an Isolation Forest to real host-worker telemetry. "
            "This does not train a failure-probability classifier."
        ),
    )
    parser.add_argument("--input", required=True, help="collected JSONL monitor output")
    parser.add_argument(
        "--output",
        default=str(DEFAULT_MODEL_PATH),
        help=f"trusted model artifact destination (default: {DEFAULT_MODEL_PATH})",
    )
    parser.add_argument(
        "--contamination",
        type=float,
        default=0.05,
        help="expected outlier fraction for Isolation Forest, in (0, 0.5]",
    )
    parser.add_argument("--random-state", type=int, default=42)
    args = parser.parse_args(argv)
    try:
        records = read_jsonl_telemetry(args.input)
        rows = telemetry_feature_rows(records)
        report = train_isolation_forest(
            rows,
            args.output,
            contamination=args.contamination,
            random_state=args.random_state,
        )
    except (OSError, ValueError, RuntimeError, ImportError) as error:
        print(f"Could not train risk model: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
