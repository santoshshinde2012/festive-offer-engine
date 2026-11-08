"""Public facade for independently testable operational use cases."""

from __future__ import annotations

from ..models import Checkout
from ..ranking import BenefitScorer
from .admission_service import AdmissionService
from .evidence_service import EvidenceService
from .models import Application, Clock, ProviderMessage, Quote
from .payment_service import PaymentService
from .policy import OfferPolicy
from .ports import EventVerifier, Provider, Record, UnitOfWorkFactory
from .publication_service import PublicationService, TrustedInputService
from .quote_service import QuoteService
from .reporting_service import ReportingService


class OfferService:
    """Expose the workflow while delegating each independent responsibility."""

    def __init__(
        self,
        units: UnitOfWorkFactory,
        provider: Provider,
        clock: Clock,
        *,
        verifier: EventVerifier,
        scorer: BenefitScorer,
        quote_seconds: int = 60,
        hold_seconds: int = 120,
        snapshot_max_age_seconds: int = 3600,
    ) -> None:
        for name, value in (
            ("quote_seconds", quote_seconds),
            ("hold_seconds", hold_seconds),
            ("snapshot_max_age_seconds", snapshot_max_age_seconds),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        policy = OfferPolicy(clock, scorer, snapshot_max_age_seconds)
        self.publication = PublicationService(units, clock)
        self.inputs = TrustedInputService(units)
        self.quotes = QuoteService(units, policy, quote_seconds)
        self.admissions = AdmissionService(units, policy, hold_seconds)
        self.payments = PaymentService(units, provider, clock)
        self.evidence = EvidenceService(units, verifier, provider, clock)
        self.reporting = ReportingService(units)

    def prepare_snapshot(
        self, bundle: Record, funding_paise: dict[str, int], per_customer_limit: int = 1
    ) -> str:
        return self.publication.prepare(bundle, funding_paise, per_customer_limit)

    def activate_snapshot(self, revision: str) -> None:
        self.publication.activate(revision)

    def seed_wallets(self, wallets: list[Record]) -> None:
        self.inputs.wallets(wallets)

    def update_checkout(self, request: Checkout, payment_id: str | None = None) -> None:
        self.inputs.checkout(request, payment_id)

    def set_campaign_enabled(self, offer_id: str, enabled: bool) -> None:
        self.publication.set_enabled(offer_id, enabled)

    def quote(self, request: Checkout) -> Quote:
        return self.quotes.quote(request)

    def apply(
        self, quote_id: str, offer_id: str, payment_id: str, idempotency_key: str
    ) -> Application:
        return self.admissions.apply(quote_id, offer_id, payment_id, idempotency_key)

    def inspect(self, application_id: str) -> Application:
        return self.payments.inspect(application_id)

    def expire_holds(self) -> int:
        return self.payments.expire_holds()

    def dispatch(self, application_id: str) -> Application:
        return self.payments.dispatch(application_id)

    def receive_event(self, message: ProviderMessage) -> str:
        return self.evidence.receive(message)

    def reconcile(self, application_id: str) -> str:
        return self.evidence.reconcile(application_id)

    def report(self) -> Record:
        return self.reporting.report()
