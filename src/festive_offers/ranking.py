from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from decimal import Decimal
from typing import Protocol

from .models import Checkout, Decision, Instrument, InstrumentRule, Offer


class BenefitScorer(Protocol):
    """Value a checked benefit in integer paise without changing nominal liability.

    Return between zero and nominal; zero is omitted from the recommendation slate.
    """

    def expected_paise(self, nominal_paise: int, probability: Decimal) -> int: ...


class ExpectedSavingsScorer:
    """Rank on expected rupees without mixing in an unrelated affinity score."""

    def expected_paise(self, nominal_paise: int, probability: Decimal) -> int:
        numerator, denominator = probability.as_integer_ratio()
        return nominal_paise * numerator // denominator


def offer_problem(offer: Offer, assumptions: Mapping[str, Decimal]) -> str | None:
    """Unknown contract terms fail closed before a discount is calculated."""
    if offer.cap_paise is None:
        return "unknown_cap"
    if offer.min_eligible_spend_paise is None:
        return "unknown_minimum"
    if not offer.instrument_rules:
        return "unknown_instrument_rules"
    if (
        offer.cap_paise < 0
        or offer.min_eligible_spend_paise < 0
        or not offer.rate.is_finite()
        or not Decimal(0) < offer.rate <= Decimal(1)
        or offer.starts_at >= offer.ends_at
        or offer.currency != "INR"
        or not offer.included_categories
        or any(not r.product or not r.network or not r.payment_mode for r in offer.instrument_rules)
    ):
        return "invalid_contract"
    if offer.payout not in {"instant", "cashback"}:
        return "unknown_payout"
    if offer.payout == "cashback":
        q = assumptions.get(offer.realization_key or "")
        if q is None or not q.is_finite() or not Decimal(0) < q <= Decimal(1):
            return "unknown_realization_probability"
    return None


def evaluate_offer(
    request: Checkout,
    offer: Offer,
    instruments: Sequence[Instrument],
    assumptions: Mapping[str, Decimal],
    run_id: str,
    scorer: BenefitScorer,
) -> Decision:
    """Evaluate one contract independently of its position among other offers."""
    reason = offer_problem(offer, assumptions)
    spend = 0
    matching: list[Instrument] = []
    if reason is None and offer.merchant != request.merchant:
        reason = "merchant_mismatch"
    if reason is None and request.currency != offer.currency:
        reason = "currency_mismatch"
    if reason is None and not offer.starts_at <= request.at < offer.ends_at:
        reason = "outside_window"
    if reason is None:
        rules = set(offer.instrument_rules)
        matching = sorted(
            (
                i
                for i in instruments
                if i.verified and InstrumentRule(i.product, i.network, i.payment_mode) in rules
            ),
            key=lambda i: i.instrument_id,
        )
        if not matching:
            reason = "no_eligible_instrument"
    if reason is None:
        spend = sum(
            item.net_amount_paise
            for item in request.items
            if item.category in offer.included_categories
            and item.brand not in offer.excluded_brands
        )
        if spend == 0:
            reason = "no_eligible_spend"
        else:
            minimum = offer.min_eligible_spend_paise
            if minimum is None:
                raise ValueError("An approved offer must declare its minimum spend")
            if spend < minimum:
                reason = "below_minimum"
    nominal = expected = 0
    probability: Decimal | None = None
    if reason is None:
        numerator, denominator = offer.rate.as_integer_ratio()
        cap = offer.cap_paise
        if cap is None:
            raise ValueError("An approved offer must declare its benefit cap")
        nominal = min(spend * numerator // denominator, cap)
        probability = (
            Decimal(1) if offer.payout == "instant" else assumptions[offer.realization_key or ""]
        )
        expected = scorer.expected_paise(nominal, probability)
        if type(expected) is not int or not 0 <= expected <= nominal:
            raise ValueError(
                "BenefitScorer must return nonnegative integer paise no greater than nominal benefit"
            )
        if nominal <= 0 or expected <= 0:
            reason = "no_positive_benefit"
    return Decision(
        run_id=run_id,
        request_id=request.request_id,
        user_id=request.user_id,
        offer_id=offer.offer_id,
        issuer=offer.issuer,
        catalog_version=offer.catalog_version,
        eligible=reason is None,
        rejection_reason=reason,
        instrument_id=matching[0].instrument_id if reason is None else None,
        eligible_spend_paise=spend,
        nominal_benefit_paise=nominal,
        expected_benefit_paise=expected,
        realization_probability=str(probability) if probability is not None else None,
        payout=offer.payout,
    )


def rank_decisions(decisions: Sequence[Decision]) -> tuple[Decision, ...]:
    """Assign deterministic ranks without changing eligibility or money."""
    ranked = sorted(
        (d for d in decisions if d.eligible),
        key=lambda d: (
            -d.expected_benefit_paise,
            -d.nominal_benefit_paise,
            d.offer_id,
            d.instrument_id,
        ),
    )
    ranks = {d.offer_id: n for n, d in enumerate(ranked, start=1)}
    return tuple(replace(d, rank=ranks.get(d.offer_id)) for d in decisions)


def rank_checkout(
    request: Checkout,
    offers: Sequence[Offer],
    instruments: Sequence[Instrument],
    assumptions: Mapping[str, Decimal],
    run_id: str,
    scorer: BenefitScorer | None = None,
) -> tuple[Decision, ...]:
    """Evaluate contracts through the scoring port, then order eligible decisions."""
    strategy = ExpectedSavingsScorer() if scorer is None else scorer
    decisions = tuple(
        evaluate_offer(request, offer, instruments, assumptions, run_id, strategy)
        for offer in offers
    )
    return rank_decisions(decisions)
