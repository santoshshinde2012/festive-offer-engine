from __future__ import annotations

import json
from dataclasses import dataclass

from ..models import Checkout
from ..serialization import dataset_from_dict
from ..validation import validate_checkout, validate_dataset
from .models import Clock, canonical, utc
from .policy import checkout_record
from .ports import Record, UnitOfWorkFactory
from .publication import verify_bundle


@dataclass(frozen=True)
class PublicationService:
    """Publish complete rule revisions without resetting financial identities."""

    units: UnitOfWorkFactory
    clock: Clock

    def prepare(self, bundle: Record, funding_paise: dict[str, int], customer_limit: int) -> str:
        verify_bundle(bundle)
        revision = bundle["manifest"]["revision"]
        ids = {row["offer_id"] for row in bundle["offers"]}
        if (
            set(funding_paise) != ids
            or any(type(v) is not int or v < 0 for v in funding_paise.values())
            or type(customer_limit) is not int
            or customer_limit < 1
        ):
            raise ValueError(
                "Every approved offer needs nonnegative integer funding and a positive usage limit"
            )
        with self.units.unit_of_work() as uow:
            now = utc(self.clock.now())
            uow.snapshots.prepare(
                {
                    "revision": revision,
                    "bundle_json": canonical(bundle),
                    "funding_json": canonical(funding_paise),
                    "customer_limit": customer_limit,
                    "status": "PREPARED",
                    "prepared_at": now,
                }
            )
            uow.events.append(None, "snapshot_prepared", bundle["manifest"], now)
        return revision

    def activate(self, revision: str) -> None:
        with self.units.unit_of_work() as uow:
            now = utc(self.clock.now())
            snapshot = uow.snapshots.get(revision)
            if snapshot is None:
                raise ValueError("Snapshot was not prepared")
            bundle = verify_bundle(json.loads(snapshot["bundle_json"]))
            funding = json.loads(snapshot["funding_json"])
            for offer in bundle["offers"]:
                offer_id = offer["offer_id"]
                original = uow.snapshots.funding(offer_id)
                if original is not None and original != funding[offer_id]:
                    raise ValueError(
                        "Publishing a revision cannot reset or change campaign funding"
                    )
                uow.snapshots.initialize_campaign(offer_id, funding[offer_id])
            uow.snapshots.activate(revision, now)
            uow.events.append(None, "snapshot_activated", {"revision": revision}, now)

    def set_enabled(self, offer_id: str, enabled: bool) -> None:
        with self.units.unit_of_work() as uow:
            if not uow.snapshots.set_enabled(offer_id, enabled):
                raise ValueError("Unknown campaign")
            uow.events.append(
                None,
                "campaign_enabled_changed",
                {"offer_id": offer_id, "enabled": enabled},
                utc(self.clock.now()),
            )


@dataclass(frozen=True)
class TrustedInputService:
    """Ingest merchant and verified-wallet records through their trusted boundary."""

    units: UnitOfWorkFactory

    def wallets(self, wallets: list[Record]) -> None:
        dataset = dataset_from_dict(
            {"offers": [], "wallets": wallets, "checkouts": [], "assumptions": {}}
        )
        validate_dataset(dataset)
        with self.units.unit_of_work() as uow:
            for wallet in wallets:
                uow.checkouts.save_wallet(wallet["user_id"], wallet["instruments"])

    def checkout(self, request: Checkout, payment_id: str | None = None) -> None:
        validate_checkout(request)
        request_json = checkout_record(request)
        with self.units.unit_of_work() as uow:
            current = uow.checkouts.checkout(request.request_id)
            selected_payment = payment_id or (
                current["payment_id"] if current else f"merchant_payment_{request.request_id}"
            )
            changed = current is not None and (
                selected_payment != current["payment_id"]
                or any(
                    json.loads(current["request_json"])[key] != request_json[key]
                    for key in request_json
                    if key != "at"
                )
            )
            if changed and uow.checkouts.is_admitted(request.request_id):
                raise ValueError("Checkout is frozen by an admitted application")
            uow.checkouts.save_checkout(
                {
                    "request_id": request.request_id,
                    "payment_id": selected_payment,
                    "request_json": canonical(request_json),
                }
            )
