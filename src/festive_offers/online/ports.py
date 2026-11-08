"""Small semantic ports used by the operational use cases."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol, TypedDict

from .models import PaymentBinding, ProviderMessage

Record = dict[str, Any]


class SnapshotRecord(TypedDict):
    """Persist one immutable prepared rule revision and its original funding."""

    revision: str
    bundle_json: str
    funding_json: str
    customer_limit: int
    status: str
    prepared_at: str


class ActiveSnapshot(TypedDict):
    """Expose the active pointer with the revision's immutable freshness origin."""

    revision: str
    bundle_json: str
    customer_limit: int
    prepared_at: str
    activated_at: str


class CheckoutRecord(TypedDict):
    """Bind trusted merchant input to exactly one registered payment identity."""

    request_id: str
    payment_id: str
    request_json: str


class QuoteRecord(TypedDict):
    """Persist the original request, wallet fingerprint and quote expiry."""

    quote_id: str
    revision: str
    payment_id: str
    request_json: str
    wallet_hash: str
    decisions_json: str
    created_at: str
    expires_at: str


class ApplicationRecord(TypedDict):
    """Carry the operational aggregate without exposing database-specific rows."""

    application_id: str
    idempotency_key: str
    fingerprint: str
    payment_id: str
    checkout_id: str
    quote_id: str
    offer_id: str
    revision: str
    user_id: str
    binding_json: str
    nominal_paise: int
    state: str
    payment_state: str
    benefit_state: str
    budget_state: str
    payout: str
    provider_reference: str
    created_at: str
    expires_at: str
    last_sequence: int
    reason: str | None


class SnapshotReadPort(Protocol):
    """Expose only the active publication required by freshness policy."""

    def active(self) -> ActiveSnapshot | None: ...


class SnapshotPort(SnapshotReadPort, Protocol):
    """Read and write rule publication state inside the caller's transaction."""

    def get(self, revision: str) -> SnapshotRecord | None: ...
    def prepare(self, record: SnapshotRecord) -> None: ...
    def funding(self, offer_id: str) -> int | None: ...
    def initialize_campaign(self, offer_id: str, funding_paise: int) -> None: ...
    def activate(self, revision: str, now: str) -> None: ...
    def set_enabled(self, offer_id: str, enabled: bool) -> bool: ...


class CheckoutPort(Protocol):
    """Keep trusted input and immutable quotes separate from financial mutation."""

    def wallet(self, user_id: str) -> list[Record]: ...
    def save_wallet(self, user_id: str, instruments: list[Record]) -> None: ...
    def checkout(self, request_id: str) -> CheckoutRecord | None: ...
    def save_checkout(self, record: CheckoutRecord) -> None: ...
    def is_admitted(self, request_id: str) -> bool: ...
    def quote(self, quote_id: str) -> QuoteRecord | None: ...
    def save_quote(self, record: QuoteRecord) -> None: ...
    def enabled_offer_ids(self) -> set[str]: ...


class AdmissionPort(Protocol):
    """Reserve only within one UoW; methods never independently commit."""

    def by_key(self, key: str) -> ApplicationRecord | None: ...
    def has_payment(self, payment_id: str) -> bool: ...
    def has_checkout(self, checkout_id: str) -> bool: ...
    def usage(self, offer_id: str, user_id: str) -> tuple[int, int]: ...
    def reserve_budget(self, offer_id: str, amount_paise: int) -> bool: ...
    def create(self, record: ApplicationRecord) -> None:
        """Save the application, increment held usage and create its READY outbox."""
        ...


class DispatchPort(Protocol):
    """Guard dispatch and release the complete held-liability aggregate."""

    def get(self, application_id: str) -> ApplicationRecord | None: ...
    def expired_ready(self, now: str) -> list[ApplicationRecord]: ...
    def release(self, application: ApplicationRecord, reason: str) -> None:
        """Move held funding to available, decrement usage, release app and cancel outbox."""
        ...

    def claim(self, application_id: str, now: str) -> bool: ...
    def finish(
        self,
        application_id: str,
        state: str,
        payment_state: str,
        outbox_state: str,
        reason: str | None,
    ) -> None: ...


class EvidencePort(Protocol):
    """Apply evidence and obligation mutations without owning transaction commits."""

    def by_reference(self, reference: str) -> ApplicationRecord | None: ...
    def inbox(self, event_id: str) -> Record | None: ...
    def save_inbox(self, record: Record, existing: bool) -> None: ...
    def mark_unknown(self, application_id: str, retained_reason: str) -> None: ...
    def qualify(self, application: ApplicationRecord, now: str) -> None:
        """Commit held funding/usage and create one OWED obligation for the application."""
        ...

    def fulfill(
        self, application_id: str, state: str, retained_reason: str | None, now: str
    ) -> None: ...
    def advance_sequence(self, application_id: str, sequence: int) -> None: ...


class EventPort(Protocol):
    """Append the financial journal in the same UoW as its corresponding mutation."""

    def append(self, application_id: str | None, kind: str, payload: Any, now: str) -> None: ...


class ReportPort(Protocol):
    """Read one consistent operational snapshot for pure replay calculations."""

    def snapshot(self) -> dict[str, list[Record]]: ...


class WorkQueuePort(Protocol):
    """Enumerate bounded work; mutation guards still decide who may dispatch."""

    def ready(self, limit: int) -> list[str]: ...
    def unresolved(self, after: str, limit: int) -> list[str]: ...


class UnitOfWork(Protocol):
    """Share one atomic boundary across repositories; no facet may commit itself."""

    @property
    def snapshots(self) -> SnapshotPort: ...
    @property
    def checkouts(self) -> CheckoutPort: ...
    @property
    def admission(self) -> AdmissionPort: ...
    @property
    def dispatch(self) -> DispatchPort: ...
    @property
    def evidence(self) -> EvidencePort: ...
    @property
    def events(self) -> EventPort: ...
    @property
    def reports(self) -> ReportPort: ...
    @property
    def work(self) -> WorkQueuePort: ...


class UnitOfWorkFactory(Protocol):
    """Commit on normal exit; roll back every facet on any BaseException."""

    def unit_of_work(self) -> AbstractContextManager[UnitOfWork]: ...


class ReportUnitOfWork(Protocol):
    """Expose only the snapshot read required by reporting."""

    @property
    def reports(self) -> ReportPort: ...


class ReportUnits(Protocol):
    """Obtain a consistent read without requiring operational mutation ports."""

    def unit_of_work(self) -> AbstractContextManager[ReportUnitOfWork]: ...


class Provider(Protocol):
    """External effects and original-reference evidence are separate operations."""

    def submit(self, reference: str, binding: PaymentBinding) -> None: ...
    def lookup(self, reference: str) -> tuple[ProviderMessage, ...]: ...


class EventVerifier(Protocol):
    """Authenticate a message without teaching business services a signing algorithm."""

    def verify(self, message: ProviderMessage) -> None: ...
