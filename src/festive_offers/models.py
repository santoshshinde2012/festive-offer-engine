from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class InstrumentRule:
    """Match the exact product, network and purchase mode in our synthetic wallet."""

    product: str
    network: str
    payment_mode: str


@dataclass(frozen=True)
class Instrument:
    """Use verified product metadata; store no card number or BIN."""

    instrument_id: str
    product: str
    network: str
    payment_mode: str
    verified: bool


@dataclass(frozen=True)
class LineItem:
    """Express spend after sale discounts and coupons in integer paise."""

    category: str
    brand: str
    net_amount_paise: int


@dataclass(frozen=True)
class Checkout:
    """Capture the basket and time for one independently ranked checkout."""

    request_id: str
    user_id: str
    merchant: str
    at: datetime
    items: tuple[LineItem, ...]
    currency: str = "INR"


@dataclass(frozen=True)
class Offer:
    """Keep synthetic contracts separate from wallet and preference data."""

    offer_id: str
    issuer: str
    merchant: str
    starts_at: datetime
    ends_at: datetime
    rate: Decimal
    min_eligible_spend_paise: int | None
    cap_paise: int | None
    instrument_rules: tuple[InstrumentRule, ...]
    included_categories: tuple[str, ...]
    excluded_brands: tuple[str, ...]
    payout: str
    realization_key: str | None = None
    currency: str = "INR"
    catalog_version: str = "synthetic-v1"


@dataclass(frozen=True)
class Dataset:
    """Hold a deliberately bounded fixture, rather than a population collect."""

    offers: tuple[Offer, ...]
    wallets: dict[str, tuple[Instrument, ...]]
    checkouts: tuple[Checkout, ...]
    assumptions: dict[str, Decimal]


@dataclass(frozen=True)
class Decision:
    """Distinguish computed decisions from offers actually shown in a UI."""

    run_id: str
    request_id: str
    user_id: str
    offer_id: str
    issuer: str
    catalog_version: str
    eligible: bool
    rejection_reason: str | None
    instrument_id: str | None
    eligible_spend_paise: int
    nominal_benefit_paise: int
    expected_benefit_paise: int
    realization_probability: str | None
    payout: str
    rank: int | None = None


@dataclass(frozen=True)
class BatchResult:
    """Publish both the ranked slate and the complete batch decision log."""

    decisions: tuple[Decision, ...]
    slate: tuple[Decision, ...]
    metrics: dict[str, object]


SCORING_VERSION = "expected_savings_v1"
