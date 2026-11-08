from __future__ import annotations

import copy
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from festive_offers.online.api import OfferApi, Principal, make_server
from festive_offers.online.fake_provider import SYNTHETIC_EVENT_KEY, FakeProvider
from festive_offers.online.models import FrozenClock, PaymentBinding
from festive_offers.online.offer_service import OfferService
from festive_offers.online.publication import prepare_bundle
from festive_offers.online.security import HmacEventVerifier
from festive_offers.online.sqlite_store import SQLiteStore
from festive_offers.online.worker import Worker
from festive_offers.ranking import ExpectedSavingsScorer

ROOT = Path(__file__).resolve().parents[1]
TOKENS = {
    role: role + "_reference_test_credential_0001"
    for role in ("customer", "other", "merchant", "operator", "provider")
}


class RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.raw = json.loads((ROOT / "data/dataset.json").read_text())
        self.checkout = self.raw["checkouts"][0]
        self.clock = FrozenClock(datetime(2026, 11, 5, 12, tzinfo=timezone.utc))
        self.store = SQLiteStore(self.root / "app.sqlite")
        self.provider = FakeProvider(self.root / "provider.sqlite")
        self.service = OfferService(
            self.store,
            self.provider,
            self.clock,
            verifier=HmacEventVerifier(SYNTHETIC_EVENT_KEY),
            scorer=ExpectedSavingsScorer(),
        )
        self.identities = {
            TOKENS[role]: Principal(
                "customer" if role == "other" else role,
                self.checkout["user_id"]
                if role == "customer"
                else "another_customer"
                if role == "other"
                else None,
            )
            for role in TOKENS
        }
        self.server = make_server(OfferApi(self.service, self.store, self.identities), port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.bundle = prepare_bundle(self.raw, "runtime_rules_v1")
        self.funding = {offer["offer_id"]: 500000 for offer in self.bundle["offers"]}

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def call(
        self,
        path: str,
        body: object = None,
        *,
        role: str = "customer",
        key: str | None = None,
        raw: bytes | None = None,
    ) -> tuple[int, dict]:
        headers = {"Authorization": f"Bearer {TOKENS[role]}"}
        if key:
            headers["Idempotency-Key"] = key
        data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self.base + path, data=data, headers=headers)
        try:
            response = urlopen(request, timeout=3)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, json.load(response)

    def initialize(self, *, customer_limit: int = 1) -> None:
        status, _ = self.call(
            "/v1/operator/publications",
            {
                "bundle": self.bundle,
                "funding_paise": self.funding,
                "customer_limit": customer_limit,
            },
            role="operator",
        )
        self.assertEqual(status, 201)
        self.assertEqual(
            self.call("/v1/operator/publications/runtime_rules_v1/activate", {}, role="operator")[
                0
            ],
            200,
        )
        self.assertEqual(
            self.call("/v1/merchant/wallets", {"wallets": self.raw["wallets"]}, role="merchant")[0],
            200,
        )
        self.assertEqual(
            self.call(
                "/v1/merchant/checkouts",
                {"checkout": self.checkout, "payment_id": "merchant_order_1"},
                role="merchant",
            )[0],
            200,
        )

    def apply(self) -> dict:
        status, quote = self.call("/v1/quotes", {"checkout_id": self.checkout["request_id"]})
        self.assertEqual(status, 201)
        self.assertEqual([d["expected_benefit_paise"] for d in quote["decisions"]], [96000, 75000])
        status, app = self.call(
            "/v1/applications",
            {"quote_id": quote["quote_id"], "offer_id": "diwali_b_cashback"},
            key="checkout_selection_1",
        )
        self.assertEqual(status, 200)
        retry = self.call(
            "/v1/applications",
            {"quote_id": quote["quote_id"], "offer_id": "diwali_b_cashback"},
            key="checkout_selection_1",
        )
        self.assertEqual(retry, (200, app))
        return app

    def test_http_worker_capture_then_separate_benefit_and_consistent_export(self) -> None:
        self.initialize()
        app = self.apply()
        worker = Worker(self.service, self.store)
        self.assertEqual(worker.run_once().dispatch_attempts, 1)
        self.assertEqual(self.provider.payment_count(), 1)
        capture = self.provider.event(app["provider_reference"], "captured")
        self.assertEqual(
            self.call("/v1/provider/events", asdict(capture), role="provider"),
            (200, {"disposition": "applied"}),
        )
        self.assertEqual(
            self.call(f"/v1/applications/{app['application_id']}")[1]["benefit_state"], "OWED"
        )
        self.provider.event(app["provider_reference"], "cashback_confirmed", benefit_paise=120000)
        self.assertEqual(worker.run_once().reconciliation_attempts, 1)
        self.assertEqual(
            self.call(f"/v1/applications/{app['application_id']}")[1]["benefit_state"], "FULFILLED"
        )
        self.assertEqual(
            self.call("/v1/provider/events", asdict(capture), role="provider")[1]["disposition"],
            "duplicate",
        )
        report = self.call("/v1/operator/report", role="operator")[1]
        self.assertTrue(
            all(row["matches_native_state"] for row in report["replay_budget_reconciliation"])
        )
        manifest = self.store.export(self.root / "export", {"replay_report": report})
        self.assertEqual(len(manifest["canonical_json_sha256"]), 14)
        self.assertEqual(len(self.store.rows("benefit_obligations")), 1)

    def test_roles_ownership_and_client_facts_cannot_cross_trusted_boundary(self) -> None:
        self.initialize()
        self.assertEqual(self.call("/v1/merchant/wallets", {"wallets": []})[0], 403)
        self.assertEqual(
            self.call("/v1/quotes", {"checkout_id": self.checkout["request_id"]}, role="other")[0],
            404,
        )
        self.assertEqual(
            self.call(
                "/v1/quotes", {"checkout_id": self.checkout["request_id"], "amount_paise": 1}
            )[0],
            400,
        )
        _, quote = self.call("/v1/quotes", {"checkout_id": self.checkout["request_id"]})
        self.assertEqual(
            self.call(
                "/v1/applications",
                {"quote_id": quote["quote_id"], "offer_id": "diwali_b_cashback"},
                role="other",
                key="stolen_quote",
            )[0],
            404,
        )
        app = self.apply()
        self.assertEqual(
            self.call(f"/v1/applications/{app['application_id']}", role="other")[0], 404
        )
        self.assertEqual(self.call("/v1/operator/report")[0], 403)
        with self.assertRaises(HTTPError) as caught:
            urlopen(self.base + "/v1/operator/report", timeout=3)
        self.assertEqual(caught.exception.code, 401)
        caught.exception.close()
        self.assertEqual(
            self.call("/v1/quotes", raw=b'{"checkout_id":"a","checkout_id":"b"}')[0], 400
        )

    def test_invalid_callback_does_not_commit_funding_or_usage(self) -> None:
        self.initialize()
        app = self.apply()
        Worker(self.service, self.store).run_once()
        message = asdict(self.provider.event(app["provider_reference"], "captured"))
        message["signature"] = "f" * 64
        self.assertEqual(self.call("/v1/provider/events", message, role="provider")[0], 400)
        state = self.service.inspect(app["application_id"])
        self.assertEqual((state.budget_state, state.payment_state), ("HELD", "PENDING"))
        self.assertEqual(self.store.rows("inbox"), [])

    def test_worker_expires_only_undispatched_holds(self) -> None:
        self.initialize()
        app = self.apply()
        self.clock.current += timedelta(seconds=121)
        result = Worker(self.service, self.store).run_once()
        self.assertEqual((result.expired, result.dispatch_attempts), (1, 0))
        self.assertEqual(self.provider.payment_count(), 0)
        self.assertEqual(self.service.inspect(app["application_id"]).state, "RELEASED")

    def test_worker_recovers_crash_after_claim_without_replacement_submission(self) -> None:
        self.initialize()
        app = self.apply()
        with self.store.unit_of_work() as uow:
            self.assertTrue(uow.dispatch.claim(app["application_id"], self.clock.now().isoformat()))
            row = uow.dispatch.get(app["application_id"])
        # Simulate acceptance at the external service immediately before process death.
        self.provider.submit(
            app["provider_reference"], PaymentBinding(**json.loads(row["binding_json"]))
        )
        self.clock.current += timedelta(seconds=121)
        worker = Worker(self.service, self.store)
        result = worker.run_once()
        self.assertEqual((result.expired, result.dispatch_attempts), (0, 0))
        self.assertEqual(self.service.inspect(app["application_id"]).budget_state, "HELD")
        self.provider.event(app["provider_reference"], "captured")
        worker.run_once()
        self.assertEqual(self.service.inspect(app["application_id"]).benefit_state, "OWED")
        with sqlite3.connect(self.provider.path) as connection:
            self.assertEqual(
                connection.execute("SELECT submission_count FROM payments").fetchone()[0], 1
            )

    def test_recovery_pages_do_not_starve_older_unknown_work(self) -> None:
        self.initialize(customer_limit=10)
        app_ids = []
        for index in range(3):
            checkout = copy.deepcopy(self.checkout)
            checkout["request_id"] += f"_{index}"
            self.call(
                "/v1/merchant/checkouts",
                {"checkout": checkout, "payment_id": f"payment_{index}"},
                role="merchant",
            )
            _, quote = self.call("/v1/quotes", {"checkout_id": checkout["request_id"]})
            _, app = self.call(
                "/v1/applications",
                {"quote_id": quote["quote_id"], "offer_id": "diwali_b_cashback"},
                key=f"key_{index}",
            )
            self.service.dispatch(app["application_id"])
            app_ids.append(app["application_id"])
        last = self.service.inspect(max(app_ids))
        self.provider.event(last.provider_reference, "captured")
        worker = Worker(self.service, self.store, batch_size=1)
        for _ in range(3):
            worker.run_once()
        self.assertEqual(self.service.inspect(last.application_id).benefit_state, "OWED")

    def test_cli_fresh_runtime_publish_seed_export_and_worker(self) -> None:
        (self.root / "bundle.json").write_text(json.dumps(self.bundle))
        (self.root / "funding.json").write_text(json.dumps(self.funding))
        env = {
            **os.environ,
            "PYTHONPATH": str(ROOT / "src"),
            "FESTIVE_PROVIDER": "fake",
            "FESTIVE_STORE": "sqlite",
            "FESTIVE_SQLITE_PATH": str(self.root / "cli.sqlite"),
            "FESTIVE_PROVIDER_DB": str(self.root / "cli_provider.sqlite"),
            "FESTIVE_SYNTHETIC_NOW": self.clock.now().isoformat(),
        }

        def run(*args: str) -> dict:
            result = subprocess.run(
                [sys.executable, "-m", "festive_offers.online.runtime", *args],
                env=env,
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            return json.loads(result.stdout)

        result = run(
            "publish",
            "--bundle",
            str(self.root / "bundle.json"),
            "--funding",
            str(self.root / "funding.json"),
            "--activate",
        )
        self.assertTrue(result["activated"])
        self.assertEqual(run("seed", "--data", str(ROOT / "data/dataset.json"))["checkouts"], 3)
        self.assertEqual(run("worker", "--once")["errors"], [])
        manifest = run("export", "--destination", str(self.root / "cli_export"))
        self.assertEqual(len(manifest["canonical_json_sha256"]), 15)
