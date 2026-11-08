from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import Clock, PaymentBinding, ProviderMessage, canonical, digest, utc
from .payment_service import release_liability
from .ports import ApplicationRecord, EventVerifier, Provider, Record, UnitOfWork, UnitOfWorkFactory

BENEFIT_KINDS = {"cashback_confirmed", "instant_discount_confirmed"}
REVIEW_REASONS = {"refund_reconcile", "binding_mismatch_reconcile"}


def validate_evidence(payload: Record) -> None:
    """Validate the synthetic normalized event independently of authentication."""
    if set(payload) != {
        "event_id",
        "provider_reference",
        "kind",
        "sequence",
        "observed_at",
        "binding",
        "benefit_paise",
    }:
        raise ValueError("Unknown synthetic provider event contract")
    if (
        type(payload["sequence"]) is not int
        or payload["sequence"] < 1
        or not payload["event_id"]
        or payload["kind"] not in BENEFIT_KINDS | {"captured", "failed", "unknown", "refunded"}
    ):
        raise ValueError("Invalid provider event identity, sequence or kind")
    if datetime.fromisoformat(payload["observed_at"]).tzinfo is None:
        raise ValueError("Provider event time must have a timezone")
    binding = PaymentBinding(**payload["binding"])
    if type(binding.amount_paise) is not int or binding.amount_paise < 0:
        raise ValueError("Provider amount must be nonnegative integer paise")


@dataclass(frozen=True)
class EvidenceService:
    """Apply authenticated evidence once without conflating capture and delivery."""

    units: UnitOfWorkFactory
    verifier: EventVerifier
    provider: Provider
    clock: Clock

    def _unknown(self, uow: UnitOfWork, application: ApplicationRecord, reason: str) -> None:
        retained = application["reason"] if application["reason"] in REVIEW_REASONS else reason
        uow.evidence.mark_unknown(application["application_id"], retained)
        uow.events.append(
            application["application_id"],
            "reconciliation_required",
            {"reason": reason},
            utc(self.clock.now()),
        )

    def _benefit(self, uow: UnitOfWork, application: ApplicationRecord, payload: Record) -> str:
        expected_kind = (
            "cashback_confirmed"
            if application["payout"] == "cashback"
            else "instant_discount_confirmed"
        )
        if (
            payload["kind"] != expected_kind
            or type(payload["benefit_paise"]) is not int
            or payload["benefit_paise"] != application["nominal_paise"]
        ):
            disposition = "benefit_evidence_reconcile"
        elif application["payment_state"] != "CAPTURED":
            disposition = "deferred_benefit"
        elif application["benefit_state"] == "FULFILLED":
            return "already_fulfilled"
        else:
            unresolved = application["reason"] in REVIEW_REASONS
            uow.evidence.fulfill(
                application["application_id"],
                "UNKNOWN" if unresolved else "FULFILLED",
                application["reason"] if unresolved else None,
                utc(self.clock.now()),
            )
            uow.events.append(
                application["application_id"],
                "benefit_fulfilled",
                {
                    "amount_paise": application["nominal_paise"],
                    "payout": application["payout"],
                },
                utc(self.clock.now()),
            )
            return "applied"
        self._unknown(uow, application, disposition)
        return disposition

    def _transition(self, uow: UnitOfWork, application: ApplicationRecord, payload: Record) -> str:
        kind = payload["kind"]
        positive_fact = (
            kind == "captured"
            and application["payment_state"] != "CAPTURED"
            and application["budget_state"] == "HELD"
        ) or (kind in BENEFIT_KINDS and application["payment_state"] == "CAPTURED")
        if payload["sequence"] <= application["last_sequence"] and not positive_fact:
            return "stale"
        if (
            application["state"] == "RELEASED"
            or (application["payment_state"] == "CAPTURED" and kind in {"failed", "unknown"})
            or (application["reason"] == "binding_mismatch_reconcile" and kind == "failed")
        ):
            return "conflict_ignored"
        if canonical(payload["binding"]) != application["binding_json"]:
            self._unknown(uow, application, "binding_mismatch_reconcile")
            return "binding_mismatch_reconcile"
        if application["payment_state"] == "NOT_SENT":
            self._unknown(uow, application, "before_dispatch_reconcile")
            return "before_dispatch_reconcile"
        if kind == "captured":
            if application["budget_state"] != "HELD":
                return "already_qualified"
            uow.evidence.qualify(application, utc(self.clock.now()))
            if application["reason"] in REVIEW_REASONS:
                # Full capture establishes the obligation; it does not settle an
                # independently observed refund or conflicting basket/instrument.
                uow.evidence.mark_unknown(application["application_id"], application["reason"])
            uow.events.append(
                application["application_id"],
                "payment_qualified_benefit_owed",
                {
                    "amount_paise": application["nominal_paise"],
                    "payout": application["payout"],
                },
                utc(self.clock.now()),
            )
            return "applied"
        if kind == "failed":
            if application["budget_state"] != "HELD":
                return "conflict_ignored"
            release_liability(
                uow, application, "provider_definitive_failure", utc(self.clock.now())
            )
            return "applied"
        if kind in BENEFIT_KINDS:
            return self._benefit(uow, application, payload)
        disposition = "refund_reconcile" if kind == "refunded" else "provider_unknown"
        self._unknown(uow, application, disposition)
        return disposition

    def receive(self, message: ProviderMessage) -> str:
        self.verifier.verify(message)
        payload = message.payload
        validate_evidence(payload)
        payload_hash = digest(payload)
        with self.units.unit_of_work() as uow:
            previous = uow.evidence.inbox(payload["event_id"])
            if previous is not None:
                if previous["payload_hash"] != payload_hash:
                    raise ValueError("Provider event ID reused with different payload")
                if previous["disposition"] != "deferred_benefit":
                    return "duplicate"
            application = uow.evidence.by_reference(payload["provider_reference"])
            if application is None:
                raise ValueError("Unknown provider payment reference")
            disposition = self._transition(uow, application, payload)
            if disposition not in {"stale", "deferred_benefit", "benefit_evidence_reconcile"}:
                uow.evidence.advance_sequence(application["application_id"], payload["sequence"])
            uow.evidence.save_inbox(
                {
                    "event_id": payload["event_id"],
                    "payload_hash": payload_hash,
                    "provider_reference": payload["provider_reference"],
                    "kind": payload["kind"],
                    "sequence": payload["sequence"],
                    "disposition": disposition,
                    "payload_json": canonical(payload),
                    "received_at": utc(self.clock.now()),
                },
                previous is not None,
            )
            uow.events.append(
                application["application_id"],
                "provider_event_received",
                {
                    "event_id": payload["event_id"],
                    "kind": payload["kind"],
                    "disposition": disposition,
                },
                utc(self.clock.now()),
            )
        return disposition

    def reconcile(self, application_id: str) -> str:
        with self.units.unit_of_work() as uow:
            application = uow.dispatch.get(application_id)
            if application is None:
                raise ValueError("Unknown application")
            reference = application["provider_reference"]
        observations = self.provider.lookup(reference)
        dispositions = [self.receive(message) for message in observations]
        if not dispositions:
            return "no_definitive_provider_evidence"
        return "applied" if "applied" in dispositions else dispositions[-1]
