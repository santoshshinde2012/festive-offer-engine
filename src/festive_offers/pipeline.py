from __future__ import annotations

from .metrics import summarize_batch
from .models import BatchResult, Dataset
from .ranking import BenefitScorer, rank_checkout
from .validation import (
    MAX_BASKET_LINES,
    MAX_INSTRUMENTS,
    MAX_OFFERS,
    MAX_REQUESTS,
    validate_dataset,
    validate_run_id,
)

# Preserve the earlier notebook imports while validation owns these limits.
__all__ = ["MAX_BASKET_LINES", "MAX_INSTRUMENTS", "MAX_OFFERS", "MAX_REQUESTS", "run_batch"]


def run_batch(dataset: Dataset, run_id: str, scorer: BenefitScorer | None = None) -> BatchResult:
    """Coordinate validated inputs, injectable scoring, and operational summaries."""
    validate_run_id(run_id)
    validate_dataset(dataset)
    decisions = tuple(
        decision
        for request in dataset.checkouts
        for decision in rank_checkout(
            request,
            dataset.offers,
            dataset.wallets.get(request.user_id, ()),
            dataset.assumptions,
            run_id,
            scorer,
        )
    )
    slate = tuple(
        sorted(
            (d for d in decisions if d.eligible),
            key=lambda d: (d.request_id, d.rank),
        )
    )
    metrics = summarize_batch(run_id, len(dataset.checkouts), decisions, slate)
    return BatchResult(decisions=decisions, slate=slate, metrics=metrics)
