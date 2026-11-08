"""Exercise the same operational contracts through native database adapters."""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import uuid
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol, cast

from festive_offers.io import load_dataset
from festive_offers.online.fake_provider import SYNTHETIC_EVENT_KEY, FakeProvider
from festive_offers.online.models import FrozenClock, PaymentBinding, digest
from festive_offers.online.offer_service import OfferService
from festive_offers.online.operational_export import EXPORT_TABLES, ExportManifest
from festive_offers.online.ports import ApplicationRecord, EventPort, UnitOfWork, UnitOfWorkFactory
from festive_offers.online.postgres_store import PostgreSQLStore
from festive_offers.online.publication import prepare_bundle
from festive_offers.online.security import HmacEventVerifier
from festive_offers.online.sqlite_store import SQLiteStore
from festive_offers.ranking import ExpectedSavingsScorer

FIXTURE = Path(__file__).resolve().parents[1] / "data" / "dataset.json"
POSTGRES_ENABLED = (
    bool(os.environ.get("FESTIVE_TEST_POSTGRES_DSN"))
    or os.environ.get("FESTIVE_TEST_EMBEDDED_POSTGRES") == "1"
)


class ContractStore(UnitOfWorkFactory, Protocol):
    """Require only the observation and export operations used by adapter checks."""

    def rows(self, table: str) -> list[dict[str, Any]]: ...
    def export(self, destination: Path, reports: Mapping[str, Any]) -> ExportManifest: ...


class AbortJournal(BaseException):
    """Represent a process-level failure that must roll back financial writes."""


class FailingJournal:
    """Fail exactly when admission journals its financial reservation."""

    def __init__(self, underlying: EventPort) -> None:
        self.underlying = underlying

    def append(self, application_id: str | None, kind: str, payload: Any, now: str) -> None:
        if kind == "liability_reserved":
            raise AbortJournal("Synthetic failure after aggregate writes")
        self.underlying.append(application_id, kind, payload, now)


class AdvancingFactory:
    """Advance time only after the underlying database lock has been acquired."""

    def __init__(self, underlying: UnitOfWorkFactory, clock: FrozenClock) -> None:
        self.underlying, self.clock = underlying, clock

    @contextmanager
    def unit_of_work(self) -> Iterator[UnitOfWork]:
        with self.underlying.unit_of_work() as unit:
            self.clock.current += timedelta(seconds=7)
            yield unit


class RepositoryContractChecks:
    """Share behavioral cases while leaving SQL and connection details native."""

    store: ContractStore
    integrity_error: type[Exception]
    root: Path

    def initialize_fixture(self, store: ContractStore, integrity_error: type[Exception]) -> None:
        self.store, self.integrity_error = store, integrity_error
        self.raw = json.loads(FIXTURE.read_text())
        self.request = load_dataset(FIXTURE).checkouts[0]
        self.clock = FrozenClock(datetime(2026, 11, 5, 12, tzinfo=timezone.utc))
        self.provider = FakeProvider(self.root / "provider.sqlite")
        self.service = self.build_service()
        self.bundle = prepare_bundle(self.raw, "repository_contract_v1")
        self.funding = {offer["offer_id"]: 500000 for offer in self.bundle["offers"]}
        self.service.prepare_snapshot(self.bundle, self.funding)
        self.service.activate_snapshot("repository_contract_v1")
        self.service.seed_wallets(self.raw["wallets"])
        self.service.update_checkout(self.request, "merchant_order_1")

    def build_service(
        self, units: UnitOfWorkFactory | None = None, **durations: Any
    ) -> OfferService:
        return OfferService(
            self.store if units is None else units,
            self.provider,
            self.clock,
            verifier=HmacEventVerifier(SYNTHETIC_EVENT_KEY),
            scorer=ExpectedSavingsScorer(),
            **durations,
        )

    def test_duplicate_online_instrument_identity_is_rejected_before_persistence(self) -> None:
        before = self.store.rows("wallets")
        wallet = copy.deepcopy(self.raw["wallets"][0])
        eligible = copy.deepcopy(wallet["instruments"][0])
        ineligible = {**eligible, "verified": False, "product": "unsupported_product"}
        wallet["instruments"] = [ineligible, eligible]
        with self.assertRaisesRegex(ValueError, "Duplicate instrument id"):
            self.service.seed_wallets([wallet])
        self.assertEqual(self.store.rows("wallets"), before)
        self.assertTrue(self.service.quote(self.request).decisions)

    def test_unsafe_or_colliding_report_names_create_no_output(self) -> None:
        for index, name in enumerate(
            ("../escaped", "/absolute", "budgets", "events", "export_manifest", "bad/name")
        ):
            destination = self.root / f"invalid_export_{index}"
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.store.export(destination, {name: {"synthetic": True}})
            self.assertFalse(destination.exists())
        self.assertFalse((self.root / "escaped.json").exists())

    def test_export_preserves_the_shared_table_inventory_and_hash_format(self) -> None:
        quote = self.service.quote(self.request)
        self.service.apply(
            quote.quote_id, "diwali_b_cashback", "merchant_order_1", "export_selection"
        )
        destination = self.root / "valid_export"
        manifest = self.store.export(destination, {"replay_report": self.service.report()})
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(
            set(manifest["canonical_json_sha256"]),
            {f"{table}.json" for table in EXPORT_TABLES} | {"replay_report.json"},
        )
        for table in EXPORT_TABLES:
            rows = json.loads((destination / f"{table}.json").read_text())
            self.assertIsInstance(rows, list)
            self.assertEqual(digest(rows), manifest["canonical_json_sha256"][f"{table}.json"])
            self.assertTrue((destination / f"{table}.csv").is_file())
        with self.assertRaises(FileExistsError):
            self.store.export(destination, {})

    def test_concurrent_exact_retry_reserves_once_through_the_factory(self) -> None:
        quote = self.service.quote(self.request)
        barrier = threading.Barrier(4)

        def attempt() -> str:
            barrier.wait(timeout=10)
            return self.service.apply(
                quote.quote_id, "diwali_b_cashback", "merchant_order_1", "shared_retry"
            ).application_id

        with ThreadPoolExecutor(max_workers=4) as pool:
            identifiers = list(pool.map(lambda _: attempt(), range(4)))
        self.assertEqual(len(set(identifiers)), 1)
        self.assertEqual(len(self.store.rows("applications")), 1)
        self.assertEqual(len(self.store.rows("outbox")), 1)
        self.assertEqual(self.store.rows("customer_usage")[0]["held_count"], 1)
        budget = next(
            row for row in self.store.rows("budgets") if row["offer_id"] == "diwali_b_cashback"
        )
        self.assertEqual(budget["held_paise"], 120000)

    def test_process_failure_rolls_back_every_admission_facet(self) -> None:
        quote = self.service.quote(self.request)
        underlying = self.store

        class FailingFactory:
            @contextmanager
            def unit_of_work(self) -> Iterator[UnitOfWork]:
                with underlying.unit_of_work() as unit:
                    # The protocol is read-only; the concrete test decorator can
                    # replace one facet while preserving the shared transaction.
                    unit.events = FailingJournal(unit.events)
                    yield unit

        with self.assertRaises(AbortJournal):
            self.build_service(FailingFactory()).apply(
                quote.quote_id, "diwali_b_cashback", "merchant_order_1", "abort"
            )
        for table in ("applications", "outbox", "customer_usage"):
            self.assertEqual(self.store.rows(table), [])
        budget = next(
            row for row in self.store.rows("budgets") if row["offer_id"] == "diwali_b_cashback"
        )
        self.assertEqual((budget["available_paise"], budget["held_paise"]), (500000, 0))
        self.assertFalse(
            any(row["kind"] == "liability_reserved" for row in self.store.rows("events"))
        )

    def test_publication_times_are_sampled_after_the_factory_acquires_its_lock(self) -> None:
        service = self.build_service(AdvancingFactory(self.store, self.clock))
        bundle = prepare_bundle(self.raw, "publication_after_lock")
        initial = self.clock.current
        service.prepare_snapshot(bundle, self.funding)
        snapshot = next(
            row
            for row in self.store.rows("snapshots")
            if row["revision"] == "publication_after_lock"
        )
        self.assertEqual(snapshot["prepared_at"], (initial + timedelta(seconds=7)).isoformat())
        service.activate_snapshot("publication_after_lock")
        self.assertEqual(
            self.store.rows("active_snapshot")[0]["activated_at"],
            (initial + timedelta(seconds=14)).isoformat(),
        )

    def test_fresh_schema_rejects_invalid_limit_funding_and_nominal_liability(self) -> None:
        with self.assertRaises(self.integrity_error):
            with self.store.unit_of_work() as unit:
                unit.snapshots.prepare(
                    {
                        "revision": "invalid_limit",
                        "bundle_json": "{}",
                        "funding_json": "{}",
                        "customer_limit": 0,
                        "status": "PREPARED",
                        "prepared_at": self.clock.current.isoformat(),
                    }
                )
        with self.assertRaises(self.integrity_error):
            with self.store.unit_of_work() as unit:
                unit.snapshots.initialize_campaign("invalid_funding", -1)
        quote = self.service.quote(self.request)
        application = self.service.apply(
            quote.quote_id, "diwali_b_cashback", "merchant_order_1", "valid_selection"
        )
        with self.store.unit_of_work() as unit:
            original = unit.dispatch.get(application.application_id)
        assert original is not None
        invalid = cast(
            ApplicationRecord,
            {
                **original,
                "application_id": "negative_application",
                "idempotency_key": "negative_key",
                "payment_id": "negative_payment",
                "checkout_id": "negative_checkout",
                "provider_reference": "negative_reference",
                "nominal_paise": -1,
            },
        )
        with self.assertRaises(self.integrity_error):
            with self.store.unit_of_work() as unit:
                unit.admission.create(invalid)
        self.assertEqual(len(self.store.rows("applications")), 1)
        self.assertEqual(len(self.store.rows("outbox")), 1)

    def test_invalid_duration_configuration_fails_before_using_the_factory(self) -> None:
        for name in ("quote_seconds", "hold_seconds", "snapshot_max_age_seconds"):
            for value in (0, -1, True, 1.5):
                with (
                    self.subTest(name=name, value=value),
                    self.assertRaisesRegex(ValueError, "positive integer"),
                ):
                    self.build_service(**{name: value})
        self.assertEqual(self.store.rows("applications"), [])


class SQLiteRepositoryContractTests(RepositoryContractChecks, unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="festive-sqlite-contract-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.initialize_fixture(
            SQLiteStore(self.root / "application.sqlite"), sqlite3.IntegrityError
        )


@unittest.skipUnless(POSTGRES_ENABLED, "Opt in to an isolated PostgreSQL integration run")
class PostgreSQLRepositoryContractTests(RepositoryContractChecks, unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            import psycopg
        except ImportError as error:
            raise unittest.SkipTest(
                "Install requirements-postgres.txt for PostgreSQL tests"
            ) from error
        cls.psycopg = psycopg
        cls.server = None
        cls.server_directory = None
        if os.environ.get("FESTIVE_TEST_POSTGRES_DSN"):
            cls.dsn = os.environ["FESTIVE_TEST_POSTGRES_DSN"]
        else:
            try:
                import pgserver
            except ImportError as error:
                raise unittest.SkipTest(
                    "Install optional pgserver for isolated PostgreSQL tests"
                ) from error
            cls.server_directory = tempfile.TemporaryDirectory(prefix="festive-pg-contract-")
            cls.server = pgserver.get_server(
                Path(cls.server_directory.name) / "pgdata", cleanup_mode="stop"
            )
            cls.dsn = cls.server.get_uri()

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.server is not None:
            cls.server.cleanup()
        if cls.server_directory is not None:
            cls.server_directory.cleanup()

    def setUp(self) -> None:
        from psycopg import sql

        temporary = tempfile.TemporaryDirectory(prefix="festive-pg-contract-fixture-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        schema = "festive_contract_" + uuid.uuid4().hex
        store = PostgreSQLStore(self.dsn, schema=schema)

        def drop_schema() -> None:
            with self.psycopg.connect(self.dsn, autocommit=True) as connection:
                connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))

        self.addCleanup(drop_schema)
        self.initialize_fixture(store, self.psycopg.IntegrityError)


class ProviderAndCompatibilityContractTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="festive-provider-contract-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.provider = FakeProvider(self.root / "provider.sqlite")
        self.binding = PaymentBinding(
            "E-Shop",
            1000000,
            "INR",
            "verified_card",
            "credit",
            "visa",
            "full",
            "merchant_checkout",
            "synthetic_basket_fingerprint",
        )

    def test_legacy_imports_refer_to_the_same_classes_constants_and_signer(self) -> None:
        from festive_offers.online import provider, service, store
        from festive_offers.online.security import sign
        from festive_offers.online.sqlite_store import SCHEMA

        self.assertIs(provider.FakeProvider, FakeProvider)
        self.assertIs(service.OfferService, OfferService)
        self.assertIs(store.SQLiteStore, SQLiteStore)
        self.assertIs(provider.sign, sign)
        self.assertIs(provider.SYNTHETIC_EVENT_KEY, SYNTHETIC_EVENT_KEY)
        self.assertIs(store.EXPORT_TABLES, EXPORT_TABLES)
        self.assertIs(store.SCHEMA, SCHEMA)

    def test_concurrent_different_bindings_cannot_share_one_provider_reference(self) -> None:
        bindings = (self.binding, replace(self.binding, amount_paise=900000))
        barrier = threading.Barrier(12)

        def submit(index: int) -> tuple[int, bool]:
            barrier.wait(timeout=10)
            try:
                self.provider.submit("same_reference", bindings[index % 2])
                return index % 2, True
            except ValueError:
                return index % 2, False

        with ThreadPoolExecutor(max_workers=12) as pool:
            outcomes = list(pool.map(submit, range(12)))
        accepted = {binding for binding, success in outcomes if success}
        self.assertEqual(len(accepted), 1)
        winning = accepted.pop()
        self.assertTrue(all(success == (binding == winning) for binding, success in outcomes))
        self.assertEqual(self.provider.payment_count(), 1)

    def test_concurrent_provider_events_allocate_unique_durable_sequences(self) -> None:
        self.provider.submit("event_reference", self.binding)
        barrier = threading.Barrier(12)

        def emit(_: int) -> int:
            barrier.wait(timeout=10)
            return self.provider.event("event_reference", "captured").payload["sequence"]

        with ThreadPoolExecutor(max_workers=12) as pool:
            sequences = list(pool.map(emit, range(12)))
        self.assertEqual(sorted(sequences), list(range(1, 13)))
        history = self.provider.lookup("event_reference")
        self.assertEqual([message.payload["sequence"] for message in history], list(range(1, 13)))
        for message in history:
            HmacEventVerifier(SYNTHETIC_EVENT_KEY).verify(message)


if __name__ == "__main__":
    unittest.main()
