from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from ..models import Checkout, Decision
from .models import Quote, canonical, digest, utc
from .policy import OfferPolicy, checkout_record
from .ports import UnitOfWorkFactory


def _required_rank(decision: Decision) -> int:
    if decision.rank is None:
        raise ValueError("An eligible offer must have a computed rank")
    return decision.rank


@dataclass(frozen=True)
class QuoteService:
    """Create bounded-lived quotes from authoritative baskets and current rules."""

    units: UnitOfWorkFactory
    policy: OfferPolicy
    quote_seconds: int

    def quote(self, request: Checkout) -> Quote:
        with self.units.unit_of_work() as uow:
            now = self.policy.clock.now()
            trusted = uow.checkouts.checkout(request.request_id)
            if trusted is None:
                raise ValueError(
                    "Merchant checkout was not ingested through the trusted input boundary"
                )
            trusted_json = json.loads(trusted["request_json"])
            supplied = checkout_record(request)
            if any(supplied[key] != trusted_json[key] for key in supplied if key != "at"):
                raise ValueError("Checkout differs from authoritative merchant input")
            request_json = {**trusted_json, "at": utc(now)}
            active, bundle = self.policy.active(uow.snapshots)
            decisions = self.policy.decisions(uow.checkouts, bundle, request_json)
            enabled = uow.checkouts.enabled_offer_ids()
            survivors = sorted(
                (d for d in decisions if d.eligible and d.offer_id in enabled), key=_required_rank
            )
            ranked = tuple(
                {**asdict(decision), "rank": rank}
                for rank, decision in enumerate(survivors, start=1)
            )
            offer_ids = {decision["offer_id"] for decision in ranked}
            ends = [
                datetime.fromisoformat(o["ends_at"].replace("Z", "+00:00"))
                for o in bundle["offers"]
                if o["offer_id"] in offer_ids
            ]
            freshness_end = datetime.fromisoformat(active["prepared_at"]) + timedelta(
                seconds=self.policy.snapshot_max_age_seconds
            )
            expires_at = utc(min(now + timedelta(seconds=self.quote_seconds), freshness_end, *ends))
            quote_id = uuid.uuid4().hex
            uow.checkouts.save_quote(
                {
                    "quote_id": quote_id,
                    "revision": active["revision"],
                    "payment_id": trusted["payment_id"],
                    "request_json": canonical(request_json),
                    "wallet_hash": digest(uow.checkouts.wallet(request.user_id)),
                    "decisions_json": canonical(ranked),
                    "created_at": utc(now),
                    "expires_at": expires_at,
                }
            )
            uow.events.append(
                None,
                "quote_created",
                {
                    "quote_id": quote_id,
                    "revision": active["revision"],
                    "request_id": request.request_id,
                },
                utc(now),
            )
        return Quote(quote_id, active["revision"], expires_at, ranked)
