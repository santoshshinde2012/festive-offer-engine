"""Shared pure policy used by quoting and transactional admission."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from ..models import Checkout, Decision
from ..ranking import BenefitScorer, rank_checkout
from ..serialization import dataset_from_dict
from .models import Application, Clock, utc
from .ports import ActiveSnapshot, ApplicationRecord, CheckoutPort, Record, SnapshotReadPort
from .publication import verify_bundle


def checkout_record(request: Checkout) -> Record:
    return {
        **asdict(request),
        "at": utc(request.at),
        "items": [asdict(item) for item in request.items],
    }


def application_value(record: ApplicationRecord) -> Application:
    return Application(
        application_id=record["application_id"],
        payment_id=record["payment_id"],
        offer_id=record["offer_id"],
        nominal_paise=record["nominal_paise"],
        state=record["state"],
        payment_state=record["payment_state"],
        benefit_state=record["benefit_state"],
        budget_state=record["budget_state"],
        provider_reference=record["provider_reference"],
    )


@dataclass(frozen=True)
class OfferPolicy:
    """Apply one freshness definition and one injected benefit objective everywhere."""

    clock: Clock
    scorer: BenefitScorer
    snapshot_max_age_seconds: int

    def active(self, snapshots: SnapshotReadPort) -> tuple[ActiveSnapshot, Record]:
        active = snapshots.active()
        if active is None or self.clock.now() >= datetime.fromisoformat(
            active["prepared_at"]
        ) + timedelta(seconds=self.snapshot_max_age_seconds):
            raise ValueError("No sufficiently fresh active snapshot")
        return active, verify_bundle(json.loads(active["bundle_json"]))

    def decisions(
        self, repository: CheckoutPort, bundle: Record, request: Record
    ) -> tuple[Decision, ...]:
        dataset = dataset_from_dict(
            {
                "offers": bundle["offers"],
                "assumptions": bundle["assumptions"],
                "checkouts": [request],
                "wallets": [
                    {
                        "user_id": request["user_id"],
                        "instruments": repository.wallet(request["user_id"]),
                    }
                ],
            }
        )
        return rank_checkout(
            dataset.checkouts[0],
            dataset.offers,
            dataset.wallets[request["user_id"]],
            dataset.assumptions,
            "online_quote",
            self.scorer,
        )
