from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .io import write_result
from .models import SCORING_VERSION
from .pipeline import run_batch
from .serialization import dataset_from_dict


def money(paise: int) -> str:
    """Format exact stored paise for the article's printed scenario."""
    return f"₹{paise // 100}.{paise % 100:02}"


def main() -> None:
    """Wire fixture input, ranking and local persistence without credentials."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data", type=Path, default=Path(__file__).resolve().parents[2] / "data" / "dataset.json"
    )
    parser.add_argument("--output", type=Path, default=Path("output"))
    parser.add_argument(
        "--run-id", default=datetime.now(timezone.utc).strftime("demo_%Y%m%dT%H%M%S_%fZ")
    )
    args = parser.parse_args()
    input_bytes = args.data.read_bytes()
    result = run_batch(dataset_from_dict(json.loads(input_bytes)), args.run_id)
    destination = args.output / args.run_id
    write_result(result, destination)
    (destination / "inputs.json").write_bytes(input_bytes)
    (destination / "run_metadata.json").write_text(
        json.dumps(
            {
                "run_id": args.run_id,
                "scoring_version": SCORING_VERSION,
                "input_sha256": hashlib.sha256(input_bytes).hexdigest(),
                "probability_scope": "synthetic assumption, not measured take-up",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        "Synthetic example; cashback probabilities are explicit assumptions, not measured take-up."
    )
    for decision in result.slate:
        print(
            f"{decision.request_id} rank={decision.rank} {decision.offer_id} "
            f"nominal={money(decision.nominal_benefit_paise)} "
            f"expected={money(decision.expected_benefit_paise)} ({decision.payout})"
        )
    print(
        f"Requests with offers: {result.metrics['requests_with_offer']}/{result.metrics['request_count']}"
    )
    print(f"Decisions: {len(result.decisions)}; ranked: {len(result.slate)}")
    print(f"First refusal reasons: {result.metrics['rejection_counts']}")
    print(f"Saved: {destination.resolve()}")


if __name__ == "__main__":
    main()
