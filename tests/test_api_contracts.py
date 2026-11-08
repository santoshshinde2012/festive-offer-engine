"""Behavioral checks for narrow API, authentication and recovery capabilities."""

from __future__ import annotations

import json
import threading
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request, urlopen

from festive_offers.io import load_dataset
from festive_offers.models import Checkout
from festive_offers.online.api import ApiError, Principal, make_server
from festive_offers.online.api_access import CustomerOfferAccess, OwnedResourceNotFoundError
from festive_offers.online.api_models import HeaderReader
from festive_offers.online.authentication import TokenAuthenticator
from festive_offers.online.models import Application, Quote, canonical
from festive_offers.online.policy import checkout_record
from festive_offers.online.ports import ApplicationRecord, CheckoutRecord, QuoteRecord, Record
from festive_offers.online.worker import Worker

FIXTURE = Path(__file__).resolve().parents[1] / "data/dataset.json"
TOKEN = "public-reference-test-customer-token"


def application_value() -> Application:
    return Application(
        "application-1",
        "merchant-payment",
        "offer-1",
        120_000,
        "HELD",
        "NOT_SENT",
        "NONE",
        "HELD",
        "original-reference",
    )


class CustomerActionsFixture:
    """Implement just the three customer operations, without a service facade."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def quote(self, request: Checkout) -> Quote:
        self.calls.append(("quote", request))
        return Quote("quote-1", "rules-1", "2026-11-05T12:01:00+00:00", ())

    def apply(
        self, quote_id: str, offer_id: str, payment_id: str, idempotency_key: str
    ) -> Application:
        self.calls.append(("apply", quote_id, offer_id, payment_id, idempotency_key))
        return application_value()

    def inspect(self, application_id: str) -> Application:
        self.calls.append(("inspect", application_id))
        return application_value()


@dataclass(frozen=True)
class CheckoutLookupFixture:
    """Provide only the two reads required by ownership, never financial writes."""

    checkout_row: CheckoutRecord
    quote_row: QuoteRecord

    def checkout(self, request_id: str) -> CheckoutRecord | None:
        return self.checkout_row if request_id == self.checkout_row["request_id"] else None

    def quote(self, quote_id: str) -> QuoteRecord | None:
        return self.quote_row if quote_id == self.quote_row["quote_id"] else None


@dataclass(frozen=True)
class ApplicationLookupFixture:
    """Return an owner-bound application without implementing dispatch."""

    owner: str

    def get(self, application_id: str) -> ApplicationRecord | None:
        if application_id != "application-1":
            return None
        return ApplicationRecord(
            application_id=application_id,
            idempotency_key="key",
            fingerprint="fingerprint",
            payment_id="merchant-payment",
            checkout_id="checkout-1",
            quote_id="quote-1",
            offer_id="offer-1",
            revision="rules-1",
            user_id=self.owner,
            binding_json="{}",
            nominal_paise=120_000,
            state="HELD",
            payment_state="NOT_SENT",
            benefit_state="NONE",
            budget_state="HELD",
            payout="cashback",
            provider_reference="original-reference",
            created_at="2026-11-05T12:00:00+00:00",
            expires_at="2026-11-05T12:02:00+00:00",
            last_sequence=0,
            reason=None,
        )


@dataclass(frozen=True)
class CustomerReadUnitFixture:
    """Deliberately omit all mutation, publication, budget and evidence facets."""

    checkouts: CheckoutLookupFixture
    dispatch: ApplicationLookupFixture


@dataclass(frozen=True)
class CustomerReadUnitsFixture:
    """Make the small read-only unit available through the context-manager port."""

    unit: CustomerReadUnitFixture

    @contextmanager
    def unit_of_work(self) -> Iterator[CustomerReadUnitFixture]:
        yield self.unit


class CustomerAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.checkout = load_dataset(FIXTURE).checkouts[0]
        self.owner = self.checkout.user_id
        payload = canonical(checkout_record(self.checkout))
        checkouts = CheckoutLookupFixture(
            CheckoutRecord(
                request_id=self.checkout.request_id,
                payment_id="merchant-payment",
                request_json=payload,
            ),
            QuoteRecord(
                quote_id="quote-1",
                revision="rules-1",
                payment_id="merchant-payment",
                request_json=payload,
                wallet_hash="wallet",
                decisions_json="[]",
                created_at="2026-11-05T12:00:00+00:00",
                expires_at="2026-11-05T12:01:00+00:00",
            ),
        )
        self.actions = CustomerActionsFixture()
        self.access = CustomerOfferAccess(
            self.actions,
            CustomerReadUnitsFixture(
                CustomerReadUnitFixture(checkouts, ApplicationLookupFixture(self.owner))
            ),
        )

    def test_customer_boundary_uses_trusted_basket_payment_and_key_namespace(self) -> None:
        self.access.quote_for_customer(self.owner, self.checkout.request_id)
        self.access.apply_for_customer(self.owner, "quote-1", "offer-1", "retry-key")
        self.access.application_for_customer(self.owner, "application-1")
        self.assertEqual(
            self.actions.calls,
            [
                ("quote", self.checkout),
                ("apply", "quote-1", "offer-1", "merchant-payment", f"{self.owner}:retry-key"),
                ("inspect", "application-1"),
            ],
        )

    def test_other_customer_and_missing_identifiers_never_reach_business_actions(self) -> None:
        for user_id, checkout_id, quote_id, application_id in (
            ("another-customer", self.checkout.request_id, "quote-1", "application-1"),
            (self.owner, "missing-checkout", "missing-quote", "missing-application"),
        ):
            with self.subTest(user_id=user_id):
                with self.assertRaisesRegex(OwnedResourceNotFoundError, "Checkout not found"):
                    self.access.quote_for_customer(user_id, checkout_id)
                with self.assertRaisesRegex(OwnedResourceNotFoundError, "Quote not found"):
                    self.access.apply_for_customer(user_id, quote_id, "offer-1", "key")
                with self.assertRaisesRegex(OwnedResourceNotFoundError, "Application not found"):
                    self.access.application_for_customer(user_id, application_id)
        self.assertEqual(self.actions.calls, [])


class AuthenticationTests(unittest.TestCase):
    def test_configured_identity_cannot_be_replaced_by_supplied_identity_text(self) -> None:
        principal = Principal("customer", "configured-customer")
        authenticator = TokenAuthenticator({TOKEN: principal})
        self.assertEqual(authenticator.authenticate("Bearer " + TOKEN), principal)
        for authorization in (
            None,
            "",
            "Basic " + TOKEN,
            "Bearer other-customer",
            "Bearer " + TOKEN + "\n",
            "Bearer é",
        ):
            with self.subTest(authorization=authorization), self.assertRaises(ApiError) as raised:
                authenticator.authenticate(authorization)
            self.assertEqual(raised.exception.status, 401)

    def test_invalid_credentials_fail_during_configuration_without_an_application(self) -> None:
        for token in ("short", "é" * 24, "x" * 24 + "\n", "x" * 24 + " "):
            with self.subTest(token=repr(token)), self.assertRaises(ValueError):
                TokenAuthenticator({token: Principal("customer", "owner")})
        with self.assertRaises(ValueError):
            TokenAuthenticator({TOKEN: Principal("customer")})
        self.assertNotIn(TOKEN, repr(TokenAuthenticator({TOKEN: Principal("customer", "owner")})))


class TransportOnlyDispatcher:
    """Require no OfferApi, database, principal map or payment implementation."""

    def handle(
        self, method: str, path: str, body: object, headers: HeaderReader
    ) -> tuple[int, Record]:
        return 202, {
            "method": method,
            "path": path,
            "test_header_received": headers.get("X-Test-Identity") == "fixture",
        }


class DispatcherSubstitutionTests(unittest.TestCase):
    def test_http_server_accepts_a_protocol_dispatcher_and_real_message_headers(self) -> None:
        with make_server(TransportOnlyDispatcher(), port=0) as server:
            thread = threading.Thread(
                target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
            )
            thread.start()
            try:
                request = Request(
                    f"http://127.0.0.1:{server.server_port}/custom-reference-endpoint",
                    headers={"X-Test-Identity": "fixture"},
                )
                with urlopen(request, timeout=2) as response:
                    self.assertEqual(response.status, 202)
                    self.assertEqual(
                        json.load(response),
                        {
                            "method": "GET",
                            "path": "/custom-reference-endpoint",
                            "test_header_received": True,
                        },
                    )
            finally:
                server.shutdown()
                thread.join(timeout=2)


class RecoveryActionsFixture:
    """Implement only expiry, dispatch and reconciliation, not the offer facade."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def expire_holds(self) -> int:
        self.calls.append(("expire",))
        return 1

    def dispatch(self, application_id: str) -> Application:
        self.calls.append(("dispatch", application_id))
        raise TimeoutError("A dispatch attempt did not complete")

    def reconcile(self, application_id: str) -> str:
        self.calls.append(("reconcile", application_id))
        return "no_definitive_provider_evidence"


class WorkQueueFixture:
    """Provide only bounded work lists; no financial mutation API is available."""

    def ready(self, limit: int) -> list[str]:
        return ["ready-application"][:limit]

    def unresolved(self, after: str, limit: int) -> list[str]:
        return ["unknown-application"][:limit] if not after else []


@dataclass(frozen=True)
class WorkUnitFixture:
    work: WorkQueueFixture


class WorkUnitsFixture:
    @contextmanager
    def unit_of_work(self) -> Iterator[WorkUnitFixture]:
        yield WorkUnitFixture(WorkQueueFixture())


class WorkerSubstitutionTests(unittest.TestCase):
    def test_dispatch_error_does_not_prevent_original_reference_recovery_work(self) -> None:
        actions = RecoveryActionsFixture()
        result = Worker(actions, WorkUnitsFixture(), batch_size=1).run_once()
        self.assertEqual(
            actions.calls,
            [("expire",), ("dispatch", "ready-application"), ("reconcile", "unknown-application")],
        )
        self.assertEqual(
            (result.expired, result.dispatch_attempts, result.reconciliation_attempts), (1, 1, 1)
        )
        self.assertEqual(
            result.errors,
            (
                {
                    "application_id": "ready-application",
                    "operation": "dispatch",
                    "error": "TimeoutError",
                },
            ),
        )


if __name__ == "__main__":
    unittest.main()
