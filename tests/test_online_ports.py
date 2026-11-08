from __future__ import annotations

import copy
import json
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from festive_offers.io import load_dataset
from festive_offers.online.fake_provider import SYNTHETIC_EVENT_KEY, FakeProvider
from festive_offers.online.models import FrozenClock, PaymentBinding, ProviderMessage, canonical
from festive_offers.online.offer_service import OfferService
from festive_offers.online.ports import Record, ReportPort, UnitOfWork, UnitOfWorkFactory
from festive_offers.online.publication import prepare_bundle
from festive_offers.online.reporting_service import ReportingService
from festive_offers.online.security import HmacEventVerifier
from festive_offers.online.sqlite_store import SQLiteStore
from festive_offers.ranking import ExpectedSavingsScorer

FIXTURE = Path(__file__).resolve().parents[1] / "data" / "dataset.json"


@dataclass(frozen=True)
class InstantPreferenceScorer:
    """A valid alternative objective changes order but cannot inflate liability."""

    def __bool__(self) -> bool:
        return False

    def expected_paise(self, nominal_paise: int, probability: Decimal) -> int:
        return nominal_paise if probability == 1 else nominal_paise // 2


class TrustedFixtureVerifier:
    """Model a different authenticated transport without depending on HMAC."""

    def verify(self, message: ProviderMessage) -> None:
        if message.signature != "authenticated-local-test-transport":
            raise ValueError("Fixture transport did not authenticate this evidence")


class TimeoutProvider:
    """Keep stable original-reference evidence after an ambiguous submission."""

    def __init__(self) -> None:
        self.payments: dict[str, PaymentBinding] = {}

    def submit(self, reference: str, binding: PaymentBinding) -> None:
        if reference in self.payments and self.payments[reference] != binding:
            raise ValueError("Changed payment for an existing reference")
        self.payments[reference] = binding
        raise TimeoutError("Accepted before a synthetic transport timeout")

    def lookup(self, reference: str) -> tuple[ProviderMessage, ...]:
        binding = self.payments.get(reference)
        if binding is None:
            return ()
        return (
            ProviderMessage(
                {
                    "event_id": reference + ":capture",
                    "provider_reference": reference,
                    "kind": "captured",
                    "sequence": 1,
                    "observed_at": "2026-11-05T12:00:00+00:00",
                    "binding": asdict(binding),
                    "benefit_paise": None,
                },
                "authenticated-local-test-transport",
            ),
        )


class FailingJournal:
    """Inject a failure after admission's financial and outbox writes."""

    def __init__(self, underlying) -> None:
        self.underlying = underlying

    def append(self, application_id, kind, payload, now) -> None:
        if kind == "liability_reserved":
            raise RuntimeError("Synthetic journal failure at the transaction boundary")
        self.underlying.append(application_id, kind, payload, now)


class DecoratedUnits:
    """Substitute only the semantic UoW contract, without exposing connection APIs."""

    def __init__(
        self,
        underlying: UnitOfWorkFactory,
        clock: FrozenClock | None = None,
        advance_seconds: int = 0,
        fail_journal: bool = False,
    ) -> None:
        self.underlying = underlying
        self.clock = clock
        self.advance_seconds = advance_seconds
        self.fail_journal = fail_journal

    @contextmanager
    def unit_of_work(self) -> Iterator[UnitOfWork]:
        with self.underlying.unit_of_work() as unit:
            if self.clock is not None:
                self.clock.current += timedelta(seconds=self.advance_seconds)
            if self.fail_journal:
                unit.events = FailingJournal(unit.events)
            yield unit


@dataclass(frozen=True)
class MemoryReports:
    """Read an independent in-memory snapshot without a SQL implementation."""

    data: dict[str, list[Record]]

    def snapshot(self) -> dict[str, list[Record]]:
        return copy.deepcopy(self.data)


@dataclass(frozen=True)
class MemoryReportUnit:
    """Expose just the read port consumed by ReportingService."""

    reports: ReportPort


@dataclass(frozen=True)
class MemoryReportUnits:
    """Provide the consistent-read contract with no operational database adapter."""

    reports: ReportPort

    @contextmanager
    def unit_of_work(self) -> Iterator[MemoryReportUnit]:
        yield MemoryReportUnit(self.reports)


class OnlinePortTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.raw = json.loads(FIXTURE.read_text())
        self.request = load_dataset(FIXTURE).checkouts[0]
        self.clock = FrozenClock(datetime(2026, 11, 5, 12, tzinfo=timezone.utc))
        self.store = SQLiteStore(self.root / "system.sqlite")
        self.provider = FakeProvider(self.root / "provider.sqlite")
        self.service = self.build()
        bundle = prepare_bundle(self.raw, "ports_v1")
        self.service.prepare_snapshot(
            bundle, {offer["offer_id"]: 500000 for offer in bundle["offers"]}
        )
        self.service.activate_snapshot("ports_v1")
        self.service.seed_wallets(self.raw["wallets"])
        self.service.update_checkout(self.request, payment_id="merchant_order_1")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def build(self, units=None, provider=None, verifier=None, scorer=None) -> OfferService:
        return OfferService(
            self.store if units is None else units,
            self.provider if provider is None else provider,
            self.clock,
            verifier=HmacEventVerifier(SYNTHETIC_EVENT_KEY) if verifier is None else verifier,
            scorer=ExpectedSavingsScorer() if scorer is None else scorer,
        )

    def test_alternate_falsy_scorer_changes_order_without_changing_maximum_liability(self) -> None:
        service = self.build(scorer=InstantPreferenceScorer())
        quote = service.quote(self.request)
        self.assertEqual(
            [d["offer_id"] for d in quote.decisions], ["diwali_a_instant", "diwali_b_cashback"]
        )
        application = service.apply(
            quote.quote_id, "diwali_b_cashback", "merchant_order_1", "ports_request"
        )
        self.assertEqual(application.nominal_paise, 120000)
        self.assertEqual(quote.decisions[1]["expected_benefit_paise"], 60000)

    def test_alternate_provider_and_verifier_preserve_timeout_and_reconciliation_contract(
        self,
    ) -> None:
        provider = TimeoutProvider()
        service = self.build(provider=provider, verifier=TrustedFixtureVerifier())
        quote = service.quote(self.request)
        application = service.apply(
            quote.quote_id, "diwali_b_cashback", "merchant_order_1", "ports_request"
        )
        self.assertEqual(service.dispatch(application.application_id).state, "UNKNOWN")
        self.assertEqual(service.reconcile(application.application_id), "applied")
        self.assertEqual(service.inspect(application.application_id).benefit_state, "OWED")
        self.assertEqual(service.reconcile(application.application_id), "duplicate")
        self.assertEqual(len(self.store.rows("benefit_obligations")), 1)
        self.assertEqual(len(provider.payments), 1)

    def test_journal_failure_rolls_back_budget_usage_application_and_outbox_together(self) -> None:
        quote = self.service.quote(self.request)
        service = self.build(units=DecoratedUnits(self.store, fail_journal=True))
        with self.assertRaisesRegex(RuntimeError, "journal failure"):
            service.apply(quote.quote_id, "diwali_b_cashback", "merchant_order_1", "ports_request")
        budget = next(
            row for row in self.store.rows("budgets") if row["offer_id"] == "diwali_b_cashback"
        )
        self.assertEqual((budget["available_paise"], budget["held_paise"]), (500000, 0))
        self.assertEqual(self.store.rows("applications"), [])
        self.assertEqual(self.store.rows("outbox"), [])
        self.assertEqual(self.store.rows("customer_usage"), [])

    def test_clock_is_sampled_after_admission_unit_acquisition(self) -> None:
        quote = self.service.quote(self.request)
        service = self.build(units=DecoratedUnits(self.store, self.clock, advance_seconds=61))
        with self.assertRaisesRegex(ValueError, "Quote expired"):
            service.apply(quote.quote_id, "diwali_b_cashback", "merchant_order_1", "ports_request")
        self.assertEqual(self.store.rows("applications"), [])

    def test_clock_is_sampled_after_dispatch_unit_acquisition(self) -> None:
        quote = self.service.quote(self.request)
        application = self.service.apply(
            quote.quote_id, "diwali_b_cashback", "merchant_order_1", "ports_request"
        )
        service = self.build(units=DecoratedUnits(self.store, self.clock, advance_seconds=121))
        self.assertEqual(service.dispatch(application.application_id).state, "RELEASED")
        self.assertEqual(self.provider.payment_count(), 0)

    def test_clock_is_sampled_after_quote_unit_acquisition(self) -> None:
        self.clock.current = datetime(2026, 11, 14, 23, 59, 50, tzinfo=timezone.utc)
        bundle = prepare_bundle(self.raw, "near_end")
        self.service.prepare_snapshot(
            bundle, {offer["offer_id"]: 500000 for offer in bundle["offers"]}
        )
        self.service.activate_snapshot("near_end")
        service = self.build(units=DecoratedUnits(self.store, self.clock, advance_seconds=20))
        self.assertEqual(service.quote(self.request).decisions, ())

    def test_reporting_accepts_an_independent_memory_read_store(self) -> None:
        snapshots = {
            "budgets": [
                {
                    "offer_id": "synthetic",
                    "funding_paise": 500,
                    "available_paise": 400,
                    "held_paise": 0,
                    "committed_paise": 100,
                }
            ],
            "applications": [{"application_id": "app", "offer_id": "synthetic", "state": "OWED"}],
            "events": [
                {
                    "application_id": "app",
                    "kind": "liability_reserved",
                    "payload_json": canonical({"amount_paise": 100}),
                },
                {
                    "application_id": "app",
                    "kind": "payment_qualified_benefit_owed",
                    "payload_json": canonical({"amount_paise": 100}),
                },
            ],
            "benefit_obligations": [
                {"application_id": "app", "state": "OWED", "amount_paise": 100}
            ],
        }
        report = ReportingService(MemoryReportUnits(MemoryReports(snapshots))).report()
        self.assertTrue(report["replay_budget_reconciliation"][0]["matches_native_state"])
        self.assertEqual(report["owed_not_fulfilled"][0]["amount_paise"], 100)

    def test_refund_review_survives_capture_delivered_out_of_order(self) -> None:
        quote = self.service.quote(self.request)
        application = self.service.apply(
            quote.quote_id, "diwali_b_cashback", "merchant_order_1", "ports_request"
        )
        self.service.dispatch(application.application_id)
        capture = self.provider.event(application.provider_reference, "captured")
        refund = self.provider.event(application.provider_reference, "refunded")
        self.service.receive_event(refund)
        self.service.receive_event(capture)
        observed = self.service.inspect(application.application_id)
        self.assertEqual((observed.state, observed.benefit_state), ("UNKNOWN", "OWED"))
        self.assertEqual(
            self.service.report()["reconciliation_required"][0]["reason"], "refund_reconcile"
        )


if __name__ == "__main__":
    unittest.main()
