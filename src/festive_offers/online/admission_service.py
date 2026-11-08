from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from .models import Application, PaymentBinding, canonical, digest, utc
from .policy import OfferPolicy, application_value
from .ports import ApplicationRecord, UnitOfWorkFactory


@dataclass(frozen=True)
class AdmissionService:
    """Reserve one order's maximum liability, usage and outbox command atomically."""

    units: UnitOfWorkFactory
    policy: OfferPolicy
    hold_seconds: int

    def apply(
        self, quote_id: str, offer_id: str, payment_id: str, idempotency_key: str
    ) -> Application:
        if not payment_id or not idempotency_key:
            raise ValueError("Stable payment identity and idempotency key are required")
        fingerprint = digest({"quote_id": quote_id, "offer_id": offer_id, "payment_id": payment_id})
        with self.units.unit_of_work() as uow:
            now = self.policy.clock.now()
            previous = uow.admission.by_key(idempotency_key)
            if previous is not None:
                if previous["fingerprint"] != fingerprint:
                    raise ValueError("Idempotency key reused with changed inputs")
                return application_value(previous)
            if uow.admission.has_payment(payment_id):
                raise ValueError("This business payment already has an application")
            quote = uow.checkouts.quote(quote_id)
            if quote is None or now >= datetime.fromisoformat(quote["expires_at"]):
                raise ValueError("Quote expired or unknown")
            active, bundle = self.policy.active(uow.snapshots)
            if active["revision"] != quote["revision"]:
                raise ValueError("Rule revision changed; obtain a new quote")
            if offer_id not in uow.checkouts.enabled_offer_ids():
                raise ValueError("Campaign disabled")
            request = json.loads(quote["request_json"])
            trusted = uow.checkouts.checkout(request["request_id"])
            if (
                trusted is None
                or payment_id != trusted["payment_id"]
                or payment_id != quote["payment_id"]
            ):
                raise ValueError(
                    "Payment identity does not match the authoritative merchant checkout"
                )
            if uow.admission.has_checkout(request["request_id"]):
                raise ValueError("This merchant checkout already has an application")
            authoritative = json.loads(trusted["request_json"])
            if any(request[key] != authoritative.get(key) for key in request if key != "at"):
                raise ValueError("Merchant basket changed; obtain a new quote")
            wallet = uow.checkouts.wallet(request["user_id"])
            if digest(wallet) != quote["wallet_hash"]:
                raise ValueError("Verified wallet changed; obtain a new quote")
            request["at"] = utc(now)
            original = {
                decision["offer_id"]: decision for decision in json.loads(quote["decisions_json"])
            }
            candidates = {
                d.offer_id: d
                for d in self.policy.decisions(uow.checkouts, bundle, request)
                if d.eligible
            }
            decision = candidates.get(offer_id)
            if (
                decision is None
                or offer_id not in original
                or decision.instrument_id != original[offer_id]["instrument_id"]
            ):
                raise ValueError(
                    "Current basket, window or verified instrument no longer qualifies"
                )
            held, committed = uow.admission.usage(offer_id, request["user_id"])
            if held + committed >= active["customer_limit"]:
                raise ValueError("Customer campaign usage exhausted")
            nominal = decision.nominal_benefit_paise
            if not uow.admission.reserve_budget(offer_id, nominal):
                raise ValueError("Campaign budget exhausted")
            instrument = next(
                item for item in wallet if item["instrument_id"] == decision.instrument_id
            )
            basket_paise = sum(item["net_amount_paise"] for item in request["items"])
            binding = PaymentBinding(
                merchant=request["merchant"],
                amount_paise=basket_paise - nominal
                if decision.payout == "instant"
                else basket_paise,
                currency=request["currency"],
                instrument_id=instrument["instrument_id"],
                product=instrument["product"],
                network=instrument["network"],
                payment_mode=instrument["payment_mode"],
                checkout_id=request["request_id"],
                basket_fingerprint=digest(
                    {key: value for key, value in request.items() if key != "at"}
                ),
            )
            app_id = uuid.uuid4().hex
            contract = next(offer for offer in bundle["offers"] if offer["offer_id"] == offer_id)
            deadline = min(
                now + timedelta(seconds=self.hold_seconds),
                datetime.fromisoformat(contract["ends_at"].replace("Z", "+00:00")),
            )
            application: ApplicationRecord = {
                "application_id": app_id,
                "idempotency_key": idempotency_key,
                "fingerprint": fingerprint,
                "payment_id": payment_id,
                "checkout_id": request["request_id"],
                "quote_id": quote_id,
                "offer_id": offer_id,
                "revision": active["revision"],
                "user_id": request["user_id"],
                "binding_json": canonical(asdict(binding)),
                "nominal_paise": nominal,
                "state": "HELD",
                "payment_state": "NOT_SENT",
                "benefit_state": "NONE",
                "budget_state": "HELD",
                "payout": decision.payout,
                "provider_reference": f"payment_{app_id}",
                "created_at": utc(now),
                "expires_at": utc(deadline),
                "last_sequence": 0,
                "reason": None,
            }
            uow.admission.create(application)
            uow.events.append(
                app_id,
                "liability_reserved",
                {
                    "offer_id": offer_id,
                    "amount_paise": nominal,
                    "payment_id": payment_id,
                },
                utc(now),
            )
            return application_value(application)
