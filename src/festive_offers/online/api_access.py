"""Customer-owned resource access without HTTP routing or concrete service adapters."""

from __future__ import annotations

import json
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol

from ..models import Checkout
from ..serialization import dataset_from_dict
from .models import Application, ProviderMessage, Quote
from .ports import ApplicationRecord, CheckoutRecord, QuoteRecord, Record


class CustomerOfferActions(Protocol):
    """Expose the three checkout operations available to the customer boundary."""

    def quote(self, request: Checkout) -> Quote: ...
    def apply(
        self, quote_id: str, offer_id: str, payment_id: str, idempotency_key: str
    ) -> Application: ...
    def inspect(self, application_id: str) -> Application: ...


class MerchantInputActions(Protocol):
    """Ingest merchant facts independently of customer selection and funding."""

    def seed_wallets(self, wallets: list[Record]) -> None: ...
    def update_checkout(self, request: Checkout, payment_id: str | None = None) -> None: ...


class CampaignActions(Protocol):
    """Prepare and control campaigns without acquiring payment capabilities."""

    def prepare_snapshot(
        self, bundle: Record, funding_paise: dict[str, int], per_customer_limit: int = 1
    ) -> str: ...
    def activate_snapshot(self, revision: str) -> None: ...
    def set_campaign_enabled(self, offer_id: str, enabled: bool) -> None: ...
    def report(self) -> Record: ...


class EvidenceActions(Protocol):
    """Accept authenticated provider evidence without exposing administrative writes."""

    def receive_event(self, message: ProviderMessage) -> str: ...


class ApiActions(
    CustomerOfferActions, MerchantInputActions, CampaignActions, EvidenceActions, Protocol
):
    """Aggregate capabilities only at the compatibility facade's composition boundary."""


class CheckoutLookup(Protocol):
    """Read only trusted checkout and immutable quote identities for ownership."""

    def checkout(self, request_id: str) -> CheckoutRecord | None: ...
    def quote(self, quote_id: str) -> QuoteRecord | None: ...


class ApplicationLookup(Protocol):
    """Read ownership without acquiring dispatch or financial mutation methods."""

    def get(self, application_id: str) -> ApplicationRecord | None: ...


class CustomerAccessUnit(Protocol):
    """Expose only the repositories required for customer-owned resource reads."""

    @property
    def checkouts(self) -> CheckoutLookup: ...
    @property
    def dispatch(self) -> ApplicationLookup: ...


class CustomerAccessUnits(Protocol):
    """Obtain protected reads without requiring the complete writable store contract."""

    def unit_of_work(self) -> AbstractContextManager[CustomerAccessUnit]: ...


class OwnedResourceNotFoundError(LookupError):
    """Hide whether an identifier is absent or belongs to another customer."""


@dataclass(frozen=True)
class CustomerOfferAccess:
    """Load server-owned facts and authorize ownership before any customer action."""

    actions: CustomerOfferActions
    units: CustomerAccessUnits

    def quote_for_customer(self, user_id: str, checkout_id: str) -> Quote:
        with self.units.unit_of_work() as unit:
            checkout_row = unit.checkouts.checkout(checkout_id)
            if checkout_row is None:
                raise OwnedResourceNotFoundError("Checkout not found")
            checkout_payload = json.loads(checkout_row["request_json"])
            if checkout_payload["user_id"] != user_id:
                raise OwnedResourceNotFoundError("Checkout not found")
            checkout = dataset_from_dict(
                {"offers": [], "wallets": [], "checkouts": [checkout_payload], "assumptions": {}}
            ).checkouts[0]
        # Quoting rechecks these loaded facts in its own protected unit of work.
        return self.actions.quote(checkout)

    def apply_for_customer(
        self, user_id: str, quote_id: str, offer_id: str, idempotency_key: str
    ) -> Application:
        with self.units.unit_of_work() as unit:
            quote_row = unit.checkouts.quote(quote_id)
            if quote_row is None or json.loads(quote_row["request_json"])["user_id"] != user_id:
                raise OwnedResourceNotFoundError("Quote not found")
            payment_id = quote_row["payment_id"]
        return self.actions.apply(quote_id, offer_id, payment_id, f"{user_id}:{idempotency_key}")

    def application_for_customer(self, user_id: str, application_id: str) -> Application:
        with self.units.unit_of_work() as unit:
            application_row = unit.dispatch.get(application_id)
            if application_row is None or application_row["user_id"] != user_id:
                raise OwnedResourceNotFoundError("Application not found")
        return self.actions.inspect(application_id)
