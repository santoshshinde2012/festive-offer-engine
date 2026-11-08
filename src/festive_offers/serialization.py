from __future__ import annotations

import csv
import json
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import StringIO
from typing import Any, Literal, overload

from .models import (
    BatchResult,
    Checkout,
    Dataset,
    Decision,
    Instrument,
    InstrumentRule,
    LineItem,
    Offer,
)
from .validation import MAX_BASKET_LINES, MAX_INSTRUMENTS, MAX_OFFERS, MAX_REQUESTS


def _only_keys(row: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(row) - allowed
    if unknown:
        raise ValueError(f"Unsupported {label} fields: {', '.join(sorted(unknown))}")


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a nonempty string")
    return value


@overload
def _integer(value: Any, field: str, nullable: Literal[False] = False) -> int: ...


@overload
def _integer(value: Any, field: str, nullable: Literal[True]) -> int | None: ...


def _integer(value: Any, field: str, nullable: bool = False) -> int | None:
    if value is None and nullable:
        return None
    if type(value) is not int:
        raise ValueError(f"{field} must be integer paise, not float or boolean")
    return value


def _decimal(value: Any, field: str) -> Decimal:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an exact decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid {field}") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


def _at(value: Any) -> datetime:
    result = datetime.fromisoformat(_text(value, "timestamp").replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamps must include an explicit UTC offset")
    return result


def _strings(values: Any, field: str) -> tuple[str, ...]:
    if not isinstance(values, list):
        raise ValueError(f"{field} must be an explicit list")
    return tuple(_text(value, field) for value in values)


def dataset_from_dict(raw: Mapping[str, Any]) -> Dataset:
    """Reject malformed input and preserve unknown cap/minimum for explicit refusal."""
    if (
        len(raw["offers"]) > MAX_OFFERS
        or len(raw["checkouts"]) > MAX_REQUESTS
        or len(raw["wallets"]) > MAX_REQUESTS
    ):
        raise ValueError("Demo limited to 100 offers, requests and wallet rows")
    if any(len(row["items"]) > MAX_BASKET_LINES for row in raw["checkouts"]):
        raise ValueError("Demo limited to 100 basket lines per checkout")
    if any(len(row["instruments"]) > MAX_INSTRUMENTS for row in raw["wallets"]):
        raise ValueError("Demo limited to 20 instruments per wallet")
    offers: list[Offer] = []
    for row in raw["offers"]:
        _only_keys(
            row,
            {
                "offer_id",
                "issuer",
                "merchant",
                "starts_at",
                "ends_at",
                "rate",
                "min_eligible_spend_paise",
                "cap_paise",
                "instrument_rules",
                "included_categories",
                "excluded_brands",
                "payout",
                "realization_key",
                "currency",
                "catalog_version",
            },
            "offer contract",
        )
        rules = row.get("instrument_rules") or []
        for rule in rules:
            _only_keys(rule, {"product", "network", "payment_mode"}, "instrument rule")
        offers.append(
            Offer(
                offer_id=_text(row["offer_id"], "offer_id"),
                issuer=_text(row["issuer"], "issuer"),
                merchant=_text(row["merchant"], "merchant"),
                starts_at=_at(row["starts_at"]),
                ends_at=_at(row["ends_at"]),
                rate=_decimal(row["rate"], "rate"),
                min_eligible_spend_paise=_integer(
                    row.get("min_eligible_spend_paise"), "minimum", nullable=True
                ),
                cap_paise=_integer(row.get("cap_paise"), "cap", nullable=True),
                instrument_rules=tuple(
                    InstrumentRule(
                        _text(r["product"], "product"),
                        _text(r["network"], "network"),
                        _text(r["payment_mode"], "payment_mode"),
                    )
                    for r in rules
                ),
                included_categories=_strings(row["included_categories"], "included_categories"),
                excluded_brands=_strings(row["excluded_brands"], "excluded_brands"),
                payout=_text(row["payout"], "payout"),
                realization_key=row.get("realization_key"),
                currency=_text(row["currency"], "currency"),
                catalog_version=_text(row["catalog_version"], "catalog_version"),
            )
        )
    wallets: dict[str, tuple[Instrument, ...]] = {}
    for row in raw["wallets"]:
        user_id = _text(row["user_id"], "user_id")
        if user_id in wallets:
            raise ValueError("Duplicate wallet user_id")
        instruments: list[Instrument] = []
        for item in row["instruments"]:
            if type(item["verified"]) is not bool:
                raise ValueError("verified must be a boolean")
            instruments.append(
                Instrument(
                    instrument_id=_text(item["instrument_id"], "instrument_id"),
                    product=_text(item["product"], "product"),
                    network=_text(item["network"], "network"),
                    payment_mode=_text(item["payment_mode"], "payment_mode"),
                    verified=item["verified"],
                )
            )
        wallets[user_id] = tuple(instruments)
    for row in raw["checkouts"]:
        for item in row["items"]:
            _only_keys(item, {"category", "brand", "net_amount_paise"}, "checkout line")
    checkouts = tuple(
        Checkout(
            request_id=_text(row["request_id"], "request_id"),
            user_id=_text(row["user_id"], "user_id"),
            merchant=_text(row["merchant"], "merchant"),
            at=_at(row["at"]),
            items=tuple(
                LineItem(
                    _text(item["category"], "category"),
                    _text(item["brand"], "brand"),
                    _integer(item["net_amount_paise"], "net_amount_paise"),
                )
                for item in row["items"]
            ),
            currency=_text(row["currency"], "currency"),
        )
        for row in raw["checkouts"]
    )
    assumptions = {key: _decimal(value, key) for key, value in raw["assumptions"].items()}
    return Dataset(tuple(offers), wallets, checkouts, assumptions)


def result_artifacts(result: BatchResult) -> dict[str, str]:
    """Encode portable results independently of any filesystem or cloud writer."""
    artifacts: dict[str, str] = {}
    for name, decisions in (("slate", result.slate), ("decisions", result.decisions)):
        rows = [asdict(decision) for decision in decisions]
        artifacts[f"{name}.json"] = json.dumps(rows, indent=2) + "\n"
        stream = StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=list(Decision.__dataclass_fields__))
        writer.writeheader()
        writer.writerows(rows)
        artifacts[f"{name}.csv"] = stream.getvalue()
    artifacts["metrics.json"] = json.dumps(result.metrics, indent=2) + "\n"
    return artifacts
