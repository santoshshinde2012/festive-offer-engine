from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from decimal import Decimal

from .models import Decision


def summarize_batch(
    run_id: str,
    request_count: int,
    decisions: Sequence[Decision],
    slate: Sequence[Decision],
) -> dict[str, object]:
    """Describe observed fixture decisions without claiming model quality or uplift."""
    tops = [decision for decision in slate if decision.rank == 1]
    issuer_counts = Counter(decision.issuer for decision in tops)
    return {
        "run_id": run_id,
        "metric_scope": "synthetic fixture operations; not model quality or uplift",
        "request_count": request_count,
        "requests_with_offer": len(tops),
        "request_coverage": str(Decimal(len(tops)) / request_count) if request_count else "0",
        "decision_count": len(decisions),
        "eligible_decision_count": len(slate),
        "rejection_counts": dict(
            sorted(
                Counter(
                    decision.rejection_reason for decision in decisions if not decision.eligible
                ).items()
            )
        ),
        "top_issuer_counts": dict(sorted(issuer_counts.items())),
        "top_issuer_share": str(Decimal(max(issuer_counts.values())) / len(tops)) if tops else "0",
    }
