"""Real PostgreSQL adapter tests, explicitly opted in; never assume a user DB.

Use FESTIVE_TEST_EMBEDDED_POSTGRES=1 with optional pgserver installed for an
isolated temporary server, or explicitly supply FESTIVE_TEST_POSTGRES_DSN for a
dedicated test database. Every test creates and removes its own random schema.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from festive_offers.io import load_dataset
from festive_offers.online.fake_provider import SYNTHETIC_EVENT_KEY, FakeProvider
from festive_offers.online.models import FrozenClock, digest
from festive_offers.online.offer_service import OfferService
from festive_offers.online.operational_export import EXPORT_TABLES
from festive_offers.online.postgres_store import PostgreSQLStore
from festive_offers.online.publication import prepare_bundle
from festive_offers.online.security import HmacEventVerifier
from festive_offers.ranking import ExpectedSavingsScorer

FIXTURE = Path(__file__).resolve().parents[1] / "data" / "dataset.json"
ENABLED = (
    bool(os.environ.get("FESTIVE_TEST_POSTGRES_DSN"))
    or os.environ.get("FESTIVE_TEST_EMBEDDED_POSTGRES") == "1"
)


class PostgreSQLConfigurationTests(unittest.TestCase):
    def test_rejects_unsafe_or_shared_schema_names_before_connecting(self) -> None:
        for schema in (
            "public",
            "pg_catalog",
            "information_schema",
            "x;DROP SCHEMA public",
            "X",
            "a" * 64,
        ):
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                PostgreSQLStore("unused", schema=schema, initialize=False)

    def test_requires_connection_and_bounded_positive_timeouts(self) -> None:
        with self.assertRaises(ValueError):
            PostgreSQLStore(initialize=False)
        for name in ("lock_timeout_ms", "statement_timeout_ms", "connect_timeout_seconds"):
            for value in (0, -1, True):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    PostgreSQLStore("unused", initialize=False, **{name: value})


@unittest.skipUnless(ENABLED, "Opt in to an isolated PostgreSQL integration run")
class PostgreSQLIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            import psycopg
        except ImportError as error:
            raise unittest.SkipTest(
                "Install requirements-postgres.txt for PostgreSQL integration tests"
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
                    "Install optional pgserver for an isolated embedded PostgreSQL run"
                ) from error
            cls.server_directory = tempfile.TemporaryDirectory(prefix="festive-pg-test-")
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
        self.temporary = tempfile.TemporaryDirectory(prefix="festive-pg-fixture-")
        self.root = Path(self.temporary.name)
        self.schema = "festive_test_" + uuid.uuid4().hex
        self.store = PostgreSQLStore(self.dsn, schema=self.schema)
        self.addCleanup(self.cleanup_schema)
        self.addCleanup(self.temporary.cleanup)
        self.raw = json.loads(FIXTURE.read_text())
        self.request = load_dataset(FIXTURE).checkouts[0]
        self.clock = FrozenClock(datetime(2026, 11, 5, 12, tzinfo=timezone.utc))
        self.provider = FakeProvider(self.root / "provider.sqlite")
        self.service = self.build()
        self.bundle = prepare_bundle(self.raw, "postgres_v1")
        self.service.prepare_snapshot(
            self.bundle, {offer["offer_id"]: 500000 for offer in self.bundle["offers"]}
        )
        self.service.activate_snapshot("postgres_v1")
        self.service.seed_wallets(self.raw["wallets"])
        self.service.update_checkout(self.request, "merchant_order_1")

    def cleanup_schema(self) -> None:
        from psycopg import sql

        with self.psycopg.connect(self.dsn, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema))
            )

    def build(self, units=None) -> OfferService:
        return OfferService(
            self.store if units is None else units,
            self.provider,
            self.clock,
            verifier=HmacEventVerifier(SYNTHETIC_EVENT_KEY),
            scorer=ExpectedSavingsScorer(),
        )

    def apply(self, key="request_one"):
        quote = self.service.quote(self.request)
        return self.service.apply(quote.quote_id, "diwali_b_cashback", "merchant_order_1", key)

    def test_capture_and_cashback_are_distinct_durable_steps_with_replay(self) -> None:
        application = self.apply()
        self.assertEqual(self.service.dispatch(application.application_id).state, "PENDING")
        capture = self.provider.event(application.provider_reference, "captured")
        self.assertEqual(self.service.receive_event(capture), "applied")
        self.assertEqual(self.service.inspect(application.application_id).benefit_state, "OWED")
        benefit = self.provider.event(
            application.provider_reference, "cashback_confirmed", benefit_paise=120000
        )
        self.assertEqual(self.service.receive_event(benefit), "applied")
        self.assertEqual(self.service.receive_event(benefit), "duplicate")
        restarted = PostgreSQLStore(self.dsn, schema=self.schema)
        self.assertEqual(
            self.build(restarted).inspect(application.application_id).state, "FULFILLED"
        )
        self.assertEqual(len(restarted.rows("benefit_obligations")), 1)
        report = self.service.report()
        self.assertTrue(
            all(row["matches_native_state"] for row in report["replay_budget_reconciliation"])
        )

    def test_real_http_and_worker_use_postgres_through_separate_benefit_delivery(self) -> None:
        from festive_offers.online.api import OfferApi, Principal, make_server
        from festive_offers.online.worker import Worker

        customer_token = "postgres_customer_test_credential_0001"
        operator_token = "postgres_operator_test_credential_0001"
        identities = {
            customer_token: Principal("customer", self.request.user_id),
            operator_token: Principal("operator"),
        }
        server = make_server(OfferApi(self.service, self.store, identities), port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def call(path, body=None, *, token=customer_token, key=None):
            headers = {"Authorization": "Bearer " + token}
            data = None if body is None else json.dumps(body).encode()
            if data is not None:
                headers["Content-Type"] = "application/json"
            if key is not None:
                headers["Idempotency-Key"] = key
            request = Request(
                f"http://127.0.0.1:{server.server_port}" + path, data=data, headers=headers
            )
            try:
                response = urlopen(request, timeout=5)
            except HTTPError as error:
                response = error
            with response:
                return response.status, json.load(response)

        try:
            status, quote = call("/v1/quotes", {"checkout_id": self.request.request_id})
            self.assertEqual(status, 201)
            self.assertEqual(
                [decision["expected_benefit_paise"] for decision in quote["decisions"]],
                [96000, 75000],
            )
            body = {"quote_id": quote["quote_id"], "offer_id": "diwali_b_cashback"}
            status, application = call("/v1/applications", body, key="http_checkout_selection")
            self.assertEqual(status, 200)
            self.assertEqual(
                call("/v1/applications", body, key="http_checkout_selection"), (200, application)
            )
            worker = Worker(self.service, self.store)
            dispatch = worker.run_once()
            self.assertEqual((dispatch.dispatch_attempts, dispatch.errors), (1, ()))
            self.assertEqual(self.provider.payment_count(), 1)
            self.provider.event(application["provider_reference"], "captured")
            self.assertEqual(worker.run_once().errors, ())
            path = "/v1/applications/" + application["application_id"]
            status, owed = call(path)
            self.assertEqual((status, owed["state"], owed["benefit_state"]), (200, "OWED", "OWED"))
            self.provider.event(
                application["provider_reference"], "cashback_confirmed", benefit_paise=120000
            )
            self.assertEqual(worker.run_once().errors, ())
            status, fulfilled = call(path)
            self.assertEqual(
                (status, fulfilled["state"], fulfilled["benefit_state"]),
                (200, "FULFILLED", "FULFILLED"),
            )
            status, report = call("/v1/operator/report", token=operator_token)
            self.assertEqual(status, 200)
            self.assertTrue(
                all(row["matches_native_state"] for row in report["replay_budget_reconciliation"])
            )
            self.assertEqual(len(self.store.rows("benefit_obligations")), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_deferred_out_of_order_benefit_is_replayed_without_duplicate_obligation(self) -> None:
        application = self.apply()
        self.service.dispatch(application.application_id)
        capture = self.provider.event(application.provider_reference, "captured")
        benefit = self.provider.event(
            application.provider_reference, "cashback_confirmed", benefit_paise=120000
        )
        self.assertEqual(self.service.receive_event(benefit), "deferred_benefit")
        self.assertEqual(self.service.receive_event(capture), "applied")
        self.assertEqual(self.service.receive_event(benefit), "applied")
        self.assertEqual(self.service.inspect(application.application_id).state, "FULFILLED")
        self.assertEqual(len(self.store.rows("benefit_obligations")), 1)

    def test_base_exception_rolls_back_financial_usage_outbox_and_journal(self) -> None:
        class Abort(BaseException):
            pass

        underlying = self.store

        class FailingUnits:
            @contextmanager
            def unit_of_work(self):
                with underlying.unit_of_work() as unit:

                    def fail(*args):
                        raise Abort("Synthetic journal failure")

                    unit.events.append = fail
                    yield unit

        quote = self.service.quote(self.request)
        with self.assertRaises(Abort):
            self.build(FailingUnits()).apply(
                quote.quote_id, "diwali_b_cashback", "merchant_order_1", "rollback"
            )
        for table in ("applications", "outbox", "customer_usage"):
            self.assertEqual(self.store.rows(table), [])
        budget = next(
            row for row in self.store.rows("budgets") if row["offer_id"] == "diwali_b_cashback"
        )
        self.assertEqual((budget["available_paise"], budget["held_paise"]), (500000, 0))

    def test_concurrent_idempotency_reuses_one_application_and_reservation(self) -> None:
        quote = self.service.quote(self.request)
        barrier = threading.Barrier(2)

        def attempt():
            barrier.wait(timeout=5)
            return self.service.apply(
                quote.quote_id, "diwali_b_cashback", "merchant_order_1", "same_key"
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            applications = list(pool.map(lambda _: attempt(), range(2)))
        self.assertEqual(applications[0], applications[1])
        self.assertEqual(len(self.store.rows("applications")), 1)
        self.assertEqual(self.store.rows("customer_usage")[0]["held_count"], 1)

    def test_concurrent_customer_limit_cannot_be_oversubscribed(self) -> None:
        second = replace(self.request, request_id="second_checkout")
        self.service.update_checkout(second, "merchant_order_2")
        quotes = [self.service.quote(request) for request in (self.request, second)]
        barrier = threading.Barrier(2)

        def attempt(index):
            barrier.wait(timeout=5)
            try:
                self.service.apply(
                    quotes[index].quote_id,
                    "diwali_b_cashback",
                    f"merchant_order_{index + 1}",
                    f"key_{index}",
                )
                return "accepted"
            except ValueError:
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(attempt, range(2))), ["accepted", "rejected"])
        self.assertEqual(self.store.rows("customer_usage")[0]["held_count"], 1)

    def test_concurrent_last_funding_reserves_the_full_nominal_amount_only_once(self) -> None:
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE budgets SET funding_paise=120000,available_paise=120000 WHERE offer_id='diwali_b_cashback'"
            )
        wallet = json.loads(json.dumps(self.raw["wallets"][0]))
        wallet["user_id"] = "second_customer"
        for instrument in wallet["instruments"]:
            instrument["instrument_id"] = "second_" + instrument["instrument_id"]
        self.service.seed_wallets([wallet])
        second = replace(self.request, request_id="second_checkout", user_id=wallet["user_id"])
        self.service.update_checkout(second, "merchant_order_2")
        quotes = [self.service.quote(request) for request in (self.request, second)]
        barrier = threading.Barrier(2)

        def attempt(index):
            barrier.wait(timeout=5)
            try:
                self.service.apply(
                    quotes[index].quote_id,
                    "diwali_b_cashback",
                    f"merchant_order_{index + 1}",
                    f"funding_key_{index}",
                )
                return "accepted"
            except ValueError:
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(attempt, range(2))), ["accepted", "rejected"])
        budget = next(
            row for row in self.store.rows("budgets") if row["offer_id"] == "diwali_b_cashback"
        )
        self.assertEqual((budget["available_paise"], budget["held_paise"]), (0, 120000))

    def test_money_uses_bigint_and_new_revision_cannot_reset_it(self) -> None:
        funding = 5_000_000_000
        with self.store.unit_of_work() as unit:
            unit.snapshots.initialize_campaign("large_campaign", funding)
            self.assertTrue(unit.admission.reserve_budget("large_campaign", 3_000_000_000))
        budget = next(
            row for row in self.store.rows("budgets") if row["offer_id"] == "large_campaign"
        )
        self.assertEqual(
            (budget["available_paise"], budget["held_paise"]), (2_000_000_000, 3_000_000_000)
        )
        with self.store.unit_of_work() as unit:
            unit.snapshots.initialize_campaign("large_campaign", 99)
            self.assertEqual(unit.snapshots.funding("large_campaign"), funding)

    def test_expiry_releases_only_unsent_work_and_cancels_outbox(self) -> None:
        application = self.apply()
        self.clock.current += timedelta(seconds=121)
        self.assertEqual(self.service.expire_holds(), 1)
        self.assertEqual(self.service.inspect(application.application_id).state, "RELEASED")
        self.assertEqual(self.store.rows("outbox")[0]["state"], "CANCELLED")
        self.assertEqual(self.provider.payment_count(), 0)

    def test_unknown_submission_reconciles_original_reference(self) -> None:
        application = self.apply()
        self.provider.timeout_after_accept = True
        self.assertEqual(self.service.dispatch(application.application_id).state, "UNKNOWN")
        self.provider.event(application.provider_reference, "captured")
        self.assertEqual(self.service.reconcile(application.application_id), "applied")
        self.assertEqual(self.service.inspect(application.application_id).benefit_state, "OWED")
        self.assertEqual(self.provider.payment_count(), 1)

    def test_work_queue_exposes_ready_and_owed_work_in_bounded_order(self) -> None:
        application = self.apply()
        with self.store.unit_of_work() as unit:
            self.assertEqual(unit.work.ready(1), [application.application_id])
            self.assertEqual(unit.work.unresolved("", 1), [])
        self.service.dispatch(application.application_id)
        self.service.receive_event(self.provider.event(application.provider_reference, "captured"))
        with self.store.unit_of_work() as unit:
            self.assertEqual(unit.work.ready(1), [])
            self.assertEqual(unit.work.unresolved("", 1), [application.application_id])
            self.assertEqual(unit.work.unresolved(application.application_id, 1), [])

    def test_export_preserves_all_thirteen_table_hash_contracts(self) -> None:
        self.apply()
        destination = self.root / "export"
        manifest = self.store.export(destination, {"replay_report": self.service.report()})
        for table in EXPORT_TABLES:
            rows = json.loads((destination / f"{table}.json").read_text())
            self.assertEqual(digest(rows), manifest["canonical_json_sha256"][f"{table}.json"])
            self.assertTrue((destination / f"{table}.csv").is_file())
        with self.assertRaises(ValueError):
            self.store.rows("applications; DELETE FROM budgets")

    def test_lock_timeout_is_bounded_and_future_transactions_can_recover(self) -> None:
        contender = PostgreSQLStore(
            self.dsn, schema=self.schema, initialize=False, lock_timeout_ms=50
        )
        with self.store.unit_of_work():
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(contender.rows, "budgets")
                with self.assertRaises(self.psycopg.errors.LockNotAvailable):
                    future.result(timeout=5)
        self.assertEqual(len(contender.rows("budgets")), len(self.bundle["offers"]))

    def test_statement_timeout_rolls_back_and_connection_factory_is_supported(self) -> None:
        factory_calls = []

        def factory():
            factory_calls.append(True)
            return self.psycopg.connect(self.dsn)

        store = PostgreSQLStore(
            connection_factory=factory,
            schema=self.schema,
            initialize=False,
            statement_timeout_ms=20,
        )
        with self.assertRaises(self.psycopg.errors.QueryCanceled):
            with store.transaction() as connection:
                connection.execute("SELECT pg_sleep(0.1)")
        self.assertEqual(len(store.rows("budgets")), len(self.bundle["offers"]))
        self.assertEqual(len(factory_calls), 2)


if __name__ == "__main__":
    unittest.main()
