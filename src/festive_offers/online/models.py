from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("A trusted timezone-aware server clock is required")
    return value.astimezone(timezone.utc).isoformat()


class Clock(Protocol):
    """Keep request timestamps separate from the application's current clock."""

    def now(self) -> datetime: ...


@dataclass(frozen=True)
class SystemClock:
    """Read current UTC time for a local service invocation."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass
class FrozenClock:
    """Advance deterministic server time explicitly in the synthetic workflow."""

    current: datetime

    def now(self) -> datetime:
        return self.current


@dataclass(frozen=True)
class PaymentBinding:
    """Bind the actual provider observation to the selected payment instrument."""

    merchant: str
    amount_paise: int
    currency: str
    instrument_id: str
    product: str
    network: str
    payment_mode: str
    checkout_id: str
    basket_fingerprint: str


@dataclass(frozen=True)
class Quote:
    """Keep a short-lived server-owned basket and eligible slate reference."""

    quote_id: str
    revision: str
    expires_at: str
    decisions: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class Application:
    """Return durable lifecycle state without implying cashback was delivered."""

    application_id: str
    payment_id: str
    offer_id: str
    nominal_paise: int
    state: str
    payment_state: str
    benefit_state: str
    budget_state: str
    provider_reference: str


@dataclass(frozen=True)
class ProviderMessage:
    """Represent an authenticated synthetic callback or provider lookup result."""

    payload: dict[str, Any]
    signature: str
