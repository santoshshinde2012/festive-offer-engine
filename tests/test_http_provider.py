"""Exercise a real loopback HTTP boundary without any real payment effects."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from dataclasses import asdict, replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from festive_offers.io import load_dataset
from festive_offers.online.fake_provider import SYNTHETIC_EVENT_KEY, FakeProvider
from festive_offers.online.http_provider import (
    HttpProvider,
    ProviderResponseError,
    ProviderTransportError,
)
from festive_offers.online.models import FrozenClock, PaymentBinding, ProviderMessage, canonical
from festive_offers.online.offer_service import OfferService
from festive_offers.online.publication import prepare_bundle
from festive_offers.online.security import HmacEventVerifier
from festive_offers.online.sqlite_store import SQLiteStore
from festive_offers.ranking import ExpectedSavingsScorer

FIXTURE = Path(__file__).resolve().parents[1] / "data" / "dataset.json"
TOKEN = "loopback-provider-fixture-token"


class ProviderFixture:
    """Expose the fake provider through the exact normalized HTTP contract."""

    def __init__(self, path: Path) -> None:
        self.provider = FakeProvider(path)
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.delay_after_accept = 0.0
        self.response_override: bytes | None = None
        self.content_type = "application/json"
        self.redirect: str | None = None
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def _send(self, status: int, body: Any) -> None:
                encoded = (
                    fixture.response_override
                    if fixture.response_override is not None
                    else canonical(body).encode()
                )
                self.send_response(status)
                self.send_header("Content-Type", fixture.content_type)
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                try:
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError):
                    # Expected when the timeout test has already stopped waiting.
                    pass

            def _authenticated(self) -> bool:
                fixture.requests.append((self.command, self.path, dict(self.headers)))
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    self._send(401, {"error": "authentication required"})
                    return False
                if fixture.redirect:
                    self.send_response(307)
                    self.send_header("Location", fixture.redirect)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return False
                return True

            def do_POST(self) -> None:
                if not self._authenticated():
                    return
                reference = self.path.rsplit("/", 1)[-1]
                try:
                    binding = PaymentBinding(
                        **json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    )
                    if self.headers.get("Idempotency-Key") != reference:
                        raise ValueError("Original reference missing")
                    fixture.provider.submit(reference, binding)
                except (TypeError, ValueError):
                    self._send(409, {"error": "conflicting payment binding"})
                    return
                time.sleep(fixture.delay_after_accept)
                self._send(202, {"provider_reference": reference, "accepted": True})

            def do_GET(self) -> None:
                if not self._authenticated():
                    return
                reference = self.path.split("/")[-2]
                messages = fixture.provider.lookup(reference)
                self._send(
                    200,
                    [
                        {"payload": message.payload, "signature": message.signature}
                        for message in messages
                    ],
                )

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True
        )
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class HttpProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixture = ProviderFixture(self.root / "provider.sqlite")
        self.adapter = HttpProvider(
            self.fixture.url, TOKEN, timeout_seconds=1, allow_insecure_loopback=True
        )
        self.binding = PaymentBinding(
            "e-shop",
            1_000_000,
            "INR",
            "verified-card",
            "credit",
            "visa",
            "online",
            "checkout-1",
            "fixture-basket-fingerprint",
        )

    def tearDown(self) -> None:
        self.fixture.close()
        self.temporary.cleanup()

    def test_timeout_after_acceptance_preserves_one_payment_and_original_reference(self) -> None:
        self.fixture.delay_after_accept = 0.15
        bounded = replace(self.adapter, timeout_seconds=0.03)
        with self.assertRaises(ProviderTransportError):
            bounded.submit("payment-original", self.binding)
        self.assertEqual(self.fixture.provider.payment_count(), 1)
        self.assertEqual(len([r for r in self.fixture.requests if r[0] == "POST"]), 1)
        self.fixture.provider.event("payment-original", "captured")
        self.fixture.provider.event("payment-original", "cashback_confirmed", benefit_paise=120_000)
        events = self.adapter.lookup("payment-original")
        self.assertEqual([event.payload["sequence"] for event in events], [1, 2])
        for event in events:
            HmacEventVerifier(SYNTHETIC_EVENT_KEY).verify(event)
        self.fixture.delay_after_accept = 0
        self.adapter.submit("payment-original", self.binding)
        self.assertEqual(self.fixture.provider.payment_count(), 1)
        with self.assertRaises(ProviderTransportError):
            self.adapter.submit("payment-original", replace(self.binding, amount_paise=1))
        self.assertEqual(self.fixture.provider.payment_count(), 1)

    def test_transport_uncertainty_then_signed_capture_and_benefit_complete_lifecycle(self) -> None:
        raw = json.loads(FIXTURE.read_text())
        store = SQLiteStore(self.root / "application.sqlite")
        service = OfferService(
            store,
            replace(self.adapter, timeout_seconds=0.03),
            FrozenClock(datetime(2026, 11, 5, 12, tzinfo=timezone.utc)),
            verifier=HmacEventVerifier(SYNTHETIC_EVENT_KEY),
            scorer=ExpectedSavingsScorer(),
        )
        bundle = prepare_bundle(raw, "http_fixture_rules")
        service.prepare_snapshot(bundle, {offer["offer_id"]: 500_000 for offer in bundle["offers"]})
        service.activate_snapshot("http_fixture_rules")
        service.seed_wallets(raw["wallets"])
        request = load_dataset(FIXTURE).checkouts[0]
        service.update_checkout(request, payment_id="http-purchase")
        quote = service.quote(request)
        application = service.apply(
            quote.quote_id, "diwali_b_cashback", "http-purchase", "http-idempotency"
        )
        self.fixture.delay_after_accept = 0.15
        self.assertEqual(service.dispatch(application.application_id).state, "UNKNOWN")
        capture = self.fixture.provider.event(application.provider_reference, "captured")
        before = store.rows("inbox")
        with self.assertRaises(ValueError):
            service.receive_event(ProviderMessage(capture.payload, "0" * 64))
        self.assertEqual(store.rows("inbox"), before)
        self.assertEqual(service.reconcile(application.application_id), "applied")
        self.assertEqual(service.inspect(application.application_id).benefit_state, "OWED")
        self.fixture.provider.event(
            application.provider_reference, "cashback_confirmed", benefit_paise=120_000
        )
        self.assertEqual(service.reconcile(application.application_id), "applied")
        self.assertEqual(service.inspect(application.application_id).state, "FULFILLED")
        self.assertEqual(self.fixture.provider.payment_count(), 1)
        budget = next(
            row for row in store.rows("budgets") if row["offer_id"] == "diwali_b_cashback"
        )
        self.assertEqual(
            (budget["available_paise"], budget["held_paise"], budget["committed_paise"]),
            (380_000, 0, 120_000),
        )
        self.assertTrue(all(row["conserved"] for row in service.report()["conservation"]))

    def test_redirect_does_not_forward_payment_or_authorization_to_another_origin(self) -> None:
        destination = ProviderFixture(self.root / "other.sqlite")
        try:
            self.fixture.redirect = destination.url + "/payments/stolen"
            with self.assertRaises(ProviderTransportError):
                self.adapter.submit("payment-original", self.binding)
            self.assertEqual(destination.requests, [])
            self.assertEqual(self.fixture.provider.payment_count(), 0)
        finally:
            destination.close()

    def test_response_size_content_type_and_malformed_json_are_rejected(self) -> None:
        self.fixture.response_override = b"[" + b" " * 200 + b"]"
        with self.assertRaisesRegex(ProviderResponseError, "byte limit"):
            replace(self.adapter, max_response_bytes=100).lookup("payment-original")
        self.fixture.response_override = b"not-json"
        with self.assertRaisesRegex(ProviderResponseError, "invalid JSON"):
            self.adapter.lookup("payment-original")
        self.fixture.response_override = b"[]"
        self.fixture.content_type = "text/html"
        with self.assertRaisesRegex(ProviderResponseError, "must be JSON"):
            self.adapter.lookup("payment-original")

    def test_wrong_acknowledgement_reference_is_not_acceptance(self) -> None:
        self.fixture.response_override = b'{"provider_reference":"another-payment","accepted":true}'
        with self.assertRaises(ProviderResponseError):
            self.adapter.submit("payment-original", self.binding)
        self.assertEqual(self.fixture.provider.payment_count(), 1)

    def test_lookup_checks_envelope_and_reference_without_trusting_signature(self) -> None:
        self.fixture.provider.submit("payment-original", self.binding)
        event = self.fixture.provider.event("payment-original", "captured")
        envelope = {"payload": event.payload, "signature": event.signature}
        invalid_values = [
            {},
            [{"payload": [], "signature": event.signature}],
            [{"payload": event.payload, "signature": "not-a-signature"}],
            [
                {
                    "payload": {**event.payload, "provider_reference": "another"},
                    "signature": event.signature,
                }
            ],
            [envelope, envelope],
            [{"payload": {**event.payload, "sequence": True}, "signature": event.signature}],
        ]
        for value in invalid_values:
            with self.subTest(value=value):
                self.fixture.response_override = canonical(value).encode()
                with self.assertRaises(ProviderResponseError):
                    self.adapter.lookup("payment-original")
        altered = {"payload": {**event.payload, "benefit_paise": 1}, "signature": event.signature}
        self.fixture.response_override = canonical([altered]).encode()
        retrieved = self.adapter.lookup("payment-original")
        with self.assertRaises(ValueError):
            HmacEventVerifier(SYNTHETIC_EVENT_KEY).verify(retrieved[0])

    def test_lookup_orders_full_history_and_does_not_treat_missing_evidence_as_failure(
        self,
    ) -> None:
        self.assertEqual(self.adapter.lookup("unknown-reference"), ())
        self.fixture.provider.submit("payment-original", self.binding)
        first = self.fixture.provider.event("payment-original", "captured")
        second = self.fixture.provider.event(
            "payment-original", "cashback_confirmed", benefit_paise=120_000
        )
        self.fixture.response_override = canonical([asdict(second), asdict(first)]).encode()
        self.assertEqual(
            [event.payload["kind"] for event in self.adapter.lookup("payment-original")],
            ["captured", "cashback_confirmed"],
        )

    def test_configuration_rejects_insecure_origins_and_unbounded_requests(self) -> None:
        invalid = [
            {"base_url": "http://example.com"},
            {"base_url": "http://127.0.0.1", "allow_insecure_loopback": False},
            {"base_url": "https://user:secret@example.com"},
            {"base_url": "https://example.com?token=secret"},
            {"base_url": "https://example.com#fragment"},
            {"base_url": "https://example.com\n"},
            {"bearer_token": ""},
            {"bearer_token": "bad\r\nheader"},
            {"timeout_seconds": float("nan")},
            {"timeout_seconds": float("inf")},
            {"timeout_seconds": 0},
            {"max_response_bytes": 0},
            {"allow_insecure_loopback": "true"},
        ]
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                replace(self.adapter, **arguments)
        self.assertNotIn(TOKEN, repr(self.adapter))
        with self.assertRaises(ValueError):
            self.adapter.lookup("unsafe\nreference")
        with self.assertRaises(ProviderTransportError):
            replace(self.adapter, bearer_token="wrong-fixture-token").lookup("payment-original")


if __name__ == "__main__":
    unittest.main()
