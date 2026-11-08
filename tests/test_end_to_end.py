from __future__ import annotations

import copy
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from festive_offers.io import load_dataset
from festive_offers.online.fake_provider import SYNTHETIC_EVENT_KEY, FakeProvider, sign
from festive_offers.online.models import FrozenClock, PaymentBinding, ProviderMessage, canonical
from festive_offers.online.offer_service import OfferService
from festive_offers.online.publication import prepare_bundle, verify_bundle
from festive_offers.online.security import HmacEventVerifier
from festive_offers.online.sqlite_store import SQLiteStore
from festive_offers.ranking import ExpectedSavingsScorer

FIXTURE = Path(__file__).resolve().parents[1] / "data" / "dataset.json"


class EndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.raw = json.loads(FIXTURE.read_text())
        self.dataset = load_dataset(FIXTURE)
        self.clock = FrozenClock(datetime(2026, 11, 5, 12, tzinfo=timezone.utc))
        self.store = SQLiteStore(self.root / "application.sqlite")
        self.provider = FakeProvider(self.root / "provider.sqlite")
        self.service = OfferService(
            self.store,
            self.provider,
            self.clock,
            verifier=HmacEventVerifier(SYNTHETIC_EVENT_KEY),
            scorer=ExpectedSavingsScorer(),
        )
        self.bundle = prepare_bundle(self.raw, "rules_v1")
        self.funding = {o["offer_id"]: 500000 for o in self.bundle["offers"]}
        self.service.prepare_snapshot(self.bundle, self.funding)
        self.service.activate_snapshot("rules_v1")
        self.service.seed_wallets(self.raw["wallets"])
        self.request = self.dataset.checkouts[0]
        self.service.update_checkout(self.request, payment_id="purchase_1")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def applied(
        self, offer: str = "diwali_b_cashback", payment: str = "purchase_1", key: str = "request_1"
    ):
        request = self.request
        if payment != "purchase_1":
            request = replace(request, request_id=f"{request.request_id}_{payment}")
            self.service.update_checkout(request, payment_id=payment)
        quote = self.service.quote(request)
        return self.service.apply(quote.quote_id, offer, payment, key)

    def captured(self):
        app = self.applied()
        self.service.dispatch(app.application_id)
        message = self.provider.event(app.provider_reference, "captured")
        self.service.receive_event(message)
        return app, message

    def budget(self, offer: str = "diwali_b_cashback"):
        return next(b for b in self.store.rows("budgets") if b["offer_id"] == offer)

    def test_source_compile_quarantines_missing_cap_and_verifies_full_snapshot(self) -> None:
        self.assertEqual(self.bundle["manifest"]["source_count"], 8)
        self.assertEqual(self.bundle["manifest"]["approved_count"], 7)
        self.assertEqual(self.bundle["quarantined"][0]["reason"], "unknown_cap")
        broken = copy.deepcopy(self.bundle)
        broken["offers"].pop()
        with self.assertRaises(ValueError):
            verify_bundle(broken)
        # Even damage after prepare cannot pass the final activation gate.
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE snapshots SET bundle_json=? WHERE revision='rules_v1'", (canonical(broken),)
            )
        with self.assertRaises(ValueError):
            self.service.activate_snapshot("rules_v1")

    def test_quote_money_and_nominal_not_expected_reservation(self) -> None:
        quote = self.service.quote(self.request)
        self.assertEqual(
            [(d["offer_id"], d["expected_benefit_paise"]) for d in quote.decisions],
            [("diwali_b_cashback", 96000), ("diwali_a_instant", 75000)],
        )
        app = self.service.apply(quote.quote_id, "diwali_b_cashback", "purchase_1", "request_1")
        self.assertEqual(app.nominal_paise, 120000)
        self.assertEqual(
            (
                self.budget()["available_paise"],
                self.budget()["held_paise"],
                self.budget()["committed_paise"],
            ),
            (380000, 120000, 0),
        )

    def test_capture_owes_cashback_separate_confirmation_fulfills(self) -> None:
        app, message = self.captured()
        state = self.service.inspect(app.application_id)
        self.assertEqual(
            (state.state, state.payment_state, state.benefit_state), ("OWED", "CAPTURED", "OWED")
        )
        self.assertEqual(self.store.rows("benefit_obligations")[0]["state"], "OWED")
        self.assertEqual(
            (self.budget()["held_paise"], self.budget()["committed_paise"]), (0, 120000)
        )
        self.assertEqual(self.service.receive_event(message), "duplicate")
        receipt = self.provider.event(
            app.provider_reference, "cashback_confirmed", benefit_paise=120000
        )
        self.service.receive_event(receipt)
        self.assertEqual(self.service.inspect(app.application_id).benefit_state, "FULFILLED")
        self.assertEqual(self.service.receive_event(receipt), "duplicate")
        self.assertEqual(len(self.store.rows("benefit_obligations")), 1)
        self.assertTrue(all(r["conserved"] for r in self.service.report()["conservation"]))

    def test_instant_binding_and_separate_fulfillment(self) -> None:
        app = self.applied("diwali_a_instant")
        self.service.dispatch(app.application_id)
        binding = json.loads(self.store.rows("applications")[0]["binding_json"])
        self.assertEqual(binding["amount_paise"], 925000)
        self.service.receive_event(self.provider.event(app.provider_reference, "captured"))
        self.assertEqual(self.service.inspect(app.application_id).benefit_state, "OWED")
        self.service.receive_event(
            self.provider.event(
                app.provider_reference, "instant_discount_confirmed", benefit_paise=75000
            )
        )
        self.assertEqual(self.service.inspect(app.application_id).benefit_state, "FULFILLED")

    def test_idempotency_and_unique_payment_survive_fulfillment_and_restart(self) -> None:
        quote = self.service.quote(self.request)
        app = self.service.apply(quote.quote_id, "diwali_b_cashback", "purchase_1", "request_1")
        self.assertEqual(
            self.service.apply(
                quote.quote_id, "diwali_b_cashback", "purchase_1", "request_1"
            ).application_id,
            app.application_id,
        )
        with self.assertRaises(ValueError):
            self.service.apply(quote.quote_id, "diwali_a_instant", "purchase_1", "request_1")
        with self.assertRaises(ValueError):
            self.service.apply(quote.quote_id, "diwali_a_instant", "purchase_1", "different_key")
        self.service.dispatch(app.application_id)
        self.service.receive_event(self.provider.event(app.provider_reference, "captured"))
        self.service.receive_event(
            self.provider.event(app.provider_reference, "cashback_confirmed", benefit_paise=120000)
        )
        restarted = OfferService(
            SQLiteStore(self.store.path),
            self.provider,
            self.clock,
            verifier=HmacEventVerifier(SYNTHETIC_EVENT_KEY),
            scorer=ExpectedSavingsScorer(),
        )
        with self.assertRaises(ValueError):
            restarted.apply(quote.quote_id, "diwali_a_instant", "purchase_1", "third_key")
        self.assertEqual(
            restarted.apply(quote.quote_id, "diwali_b_cashback", "purchase_1", "request_1").state,
            "FULFILLED",
        )
        self.assertEqual(self.budget()["committed_paise"], 120000)

    def test_trusted_basket_and_wallet_changes_require_requote(self) -> None:
        quote = self.service.quote(self.request)
        changed = replace(
            self.request, items=(replace(self.request.items[0], net_amount_paise=2000000),)
        )
        self.service.update_checkout(changed)
        with self.assertRaisesRegex(ValueError, "basket changed"):
            self.service.apply(quote.quote_id, "diwali_b_cashback", "purchase_1", "key")
        self.service.update_checkout(self.request)
        wallet = copy.deepcopy(self.raw["wallets"][0])
        wallet["instruments"][0]["network"] = "mastercard"
        self.service.seed_wallets([wallet])
        with self.assertRaisesRegex(ValueError, "wallet changed"):
            self.service.apply(quote.quote_id, "diwali_b_cashback", "purchase_1", "key2")
        self.assertEqual(self.budget()["held_paise"], 0)

    def test_server_clock_overrides_client_time_and_enforces_expiry_window(self) -> None:
        quote = self.service.quote(
            replace(self.request, at=datetime(2099, 1, 1, tzinfo=timezone.utc))
        )
        self.assertTrue(quote.decisions)
        self.clock.current += timedelta(seconds=61)
        with self.assertRaisesRegex(ValueError, "Quote expired"):
            self.service.apply(quote.quote_id, "diwali_b_cashback", "p", "k")
        self.clock.current = datetime(2026, 11, 15, tzinfo=timezone.utc)
        # A genuinely new publication is fresh; the contract end still excludes this time.
        self.service.prepare_snapshot(prepare_bundle(self.raw, "rules_after_window"), self.funding)
        self.service.activate_snapshot("rules_after_window")
        self.assertEqual(self.service.quote(self.request).decisions, ())

    def test_quote_expiry_is_bounded_by_window_and_immutable_source_freshness(self) -> None:
        initial = self.clock.current
        self.clock.current = initial + timedelta(seconds=3590)
        self.assertEqual(
            datetime.fromisoformat(self.service.quote(self.request).expires_at),
            initial + timedelta(seconds=3600),
        )
        self.service.activate_snapshot("rules_v1")
        self.clock.current = initial + timedelta(seconds=3601)
        with self.assertRaisesRegex(ValueError, "fresh"):
            self.service.quote(self.request)
        self.clock.current = datetime(2026, 11, 14, 23, 59, 50, tzinfo=timezone.utc)
        self.service.prepare_snapshot(prepare_bundle(self.raw, "near_end"), self.funding)
        self.service.activate_snapshot("near_end")
        quote = self.service.quote(self.request)
        self.assertEqual(
            datetime.fromisoformat(quote.expires_at), datetime(2026, 11, 15, tzinfo=timezone.utc)
        )

    def test_admitted_merchant_basket_is_frozen(self) -> None:
        self.applied()
        changed = replace(
            self.request, items=(replace(self.request.items[0], net_amount_paise=500000),)
        )
        with self.assertRaisesRegex(ValueError, "frozen"):
            self.service.update_checkout(changed)

    def test_online_ingestion_rejects_empty_and_negative_net_baskets(self) -> None:
        for request in (
            replace(self.request, items=()),
            replace(
                self.request, items=(replace(self.request.items[0], net_amount_paise=-1000000),)
            ),
        ):
            with self.assertRaises(ValueError):
                self.service.update_checkout(request)

    def test_reordered_benefit_and_lost_capture_recover_without_duplicate_effect(self) -> None:
        app = self.applied()
        self.service.dispatch(app.application_id)
        capture = self.provider.event(app.provider_reference, "captured")
        receipt = self.provider.event(
            app.provider_reference, "cashback_confirmed", benefit_paise=120000
        )
        self.assertEqual(self.service.receive_event(receipt), "deferred_benefit")
        self.assertEqual(self.budget()["held_paise"], 120000)
        # The capture callback was lost. Lookup returns authoritative evidence for
        # both payment and benefit, rather than returning only the last callback.
        self.assertEqual(self.service.reconcile(app.application_id), "applied")
        self.assertEqual(self.service.inspect(app.application_id).benefit_state, "FULFILLED")
        self.assertEqual(self.budget()["committed_paise"], 120000)
        self.assertEqual(self.service.receive_event(capture), "duplicate")
        self.assertEqual(self.service.receive_event(receipt), "duplicate")
        self.assertEqual(len(self.store.rows("benefit_obligations")), 1)

    def test_refund_reconciliation_survives_valid_benefit_confirmation(self) -> None:
        app, _ = self.captured()
        self.service.receive_event(self.provider.event(app.provider_reference, "refunded"))
        self.service.receive_event(
            self.provider.event(app.provider_reference, "cashback_confirmed", benefit_paise=120000)
        )
        state = self.service.inspect(app.application_id)
        self.assertEqual((state.state, state.benefit_state), ("UNKNOWN", "FULFILLED"))
        self.assertEqual(
            self.service.report()["reconciliation_required"][0]["reason"], "refund_reconcile"
        )
        self.assertEqual(self.budget()["committed_paise"], 120000)

    def test_dispatch_claim_is_committed_before_external_call_and_crash_stays_held(self) -> None:
        app = self.applied()
        original = self.provider.submit

        def crash_after_provider_accept(reference, binding):
            # Acquiring this writer transaction would fail if the service still
            # held its admission/claim transaction across the external call.
            with self.store.transaction() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT state FROM outbox WHERE application_id=?", (app.application_id,)
                    ).fetchone()[0],
                    "CLAIMED",
                )
            original(reference, binding)
            raise KeyboardInterrupt("Synthetic process crash after provider effect")

        self.provider.submit = crash_after_provider_accept
        with self.assertRaises(KeyboardInterrupt):
            self.service.dispatch(app.application_id)
        self.assertEqual(self.service.inspect(app.application_id).state, "DISPATCHING")
        self.clock.current += timedelta(seconds=121)
        self.assertEqual(self.service.expire_holds(), 0)
        self.assertEqual(self.budget()["held_paise"], 120000)
        self.provider.event(app.provider_reference, "captured")
        self.assertEqual(self.service.reconcile(app.application_id), "applied")
        self.assertEqual(self.provider.payment_count(), 1)

    def test_disable_and_revision_gate_preserve_financial_history(self) -> None:
        app = self.applied()
        second_quote = self.service.quote(self.request)
        self.service.set_campaign_enabled("diwali_a_instant", False)
        with self.assertRaisesRegex(ValueError, "disabled"):
            self.service.apply(second_quote.quote_id, "diwali_a_instant", "p2", "k2")
        bundle2 = prepare_bundle(self.raw, "rules_v2")
        self.service.prepare_snapshot(bundle2, self.funding)
        self.service.activate_snapshot("rules_v2")
        with self.assertRaisesRegex(ValueError, "revision changed"):
            self.service.apply(second_quote.quote_id, "diwali_b_cashback", "p3", "k3")
        self.assertEqual(self.budget()["held_paise"], 120000)
        self.assertEqual(len(self.store.rows("snapshots")), 2)
        self.service.dispatch(app.application_id)
        self.service.receive_event(self.provider.event(app.provider_reference, "captured"))
        self.assertEqual(self.budget()["committed_paise"], 120000)

    def test_new_revision_cannot_reset_funding_or_customer_limit(self) -> None:
        self.captured()
        bundle2 = prepare_bundle(self.raw, "rules_v2")
        self.service.prepare_snapshot(bundle2, self.funding)
        self.service.activate_snapshot("rules_v2")
        with self.assertRaisesRegex(ValueError, "usage exhausted"):
            self.applied(payment="another", key="another")
        funding = {**self.funding, "diwali_b_cashback": 1000000}
        self.service.prepare_snapshot(prepare_bundle(self.raw, "rules_v3"), funding)
        with self.assertRaisesRegex(ValueError, "cannot reset"):
            self.service.activate_snapshot("rules_v3")

    def test_concurrent_last_budget_reservations_fund_only_one(self) -> None:
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE budgets SET funding_paise=120000,available_paise=120000 WHERE offer_id='diwali_b_cashback'"
            )
        bob_wallet = copy.deepcopy(self.raw["wallets"][0])
        bob_wallet["user_id"] = "bob"
        for instrument in bob_wallet["instruments"]:
            instrument["instrument_id"] = "bob_" + instrument["instrument_id"]
        self.service.seed_wallets([bob_wallet])
        bob = replace(self.request, request_id="bob_checkout", user_id="bob")
        self.service.update_checkout(self.request, payment_id="p0")
        self.service.update_checkout(bob, payment_id="p1")
        quotes = [self.service.quote(self.request), self.service.quote(bob)]

        def attempt(index):
            try:
                return self.service.apply(
                    quotes[index].quote_id, "diwali_b_cashback", f"p{index}", f"k{index}"
                ).state
            except ValueError:
                return "REFUSED"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(attempt, range(2)))
        self.assertEqual(sorted(outcomes), ["HELD", "REFUSED"])
        self.assertEqual(
            (self.budget()["available_paise"], self.budget()["held_paise"]), (0, 120000)
        )
        self.assertTrue(
            all(
                u["held_count"] >= 0 and u["committed_count"] >= 0
                for u in self.store.rows("customer_usage")
            )
        )

    def test_expired_hold_never_dispatches_but_inflight_timeout_stays_held(self) -> None:
        app = self.applied()
        self.clock.current += timedelta(seconds=121)
        self.assertEqual(self.service.dispatch(app.application_id).state, "RELEASED")
        self.assertEqual(self.provider.payment_count(), 0)
        self.assertEqual(self.budget()["available_paise"], 500000)
        app2 = self.applied(payment="p2", key="k2")
        self.provider.timeout_after_accept = True
        self.assertEqual(self.service.dispatch(app2.application_id).state, "UNKNOWN")
        self.clock.current += timedelta(seconds=121)
        self.assertEqual(self.service.expire_holds(), 0)
        self.assertEqual(self.budget()["held_paise"], 120000)
        self.assertEqual(
            self.service.reconcile(app2.application_id), "no_definitive_provider_evidence"
        )
        self.service.dispatch(app2.application_id)
        self.assertEqual(self.provider.payment_count(), 1)
        self.provider.event(app2.provider_reference, "captured")
        self.assertEqual(self.service.reconcile(app2.application_id), "applied")
        self.assertEqual(self.service.inspect(app2.application_id).benefit_state, "OWED")

    def test_definitive_failure_releases_once_and_late_capture_cannot_regress(self) -> None:
        app = self.applied()
        self.service.dispatch(app.application_id)
        failed = self.provider.event(app.provider_reference, "failed")
        self.service.receive_event(failed)
        self.assertEqual(self.budget()["available_paise"], 500000)
        self.assertEqual(self.service.receive_event(failed), "duplicate")
        self.assertEqual(
            self.service.receive_event(self.provider.event(app.provider_reference, "captured")),
            "conflict_ignored",
        )
        self.assertEqual(self.budget()["held_paise"], 0)
        self.assertEqual(self.store.rows("customer_usage")[0]["held_count"], 0)

    def test_binding_and_partial_capture_are_held_for_reconciliation(self) -> None:
        for field, value in (
            ("amount_paise", 500000),
            ("currency", "USD"),
            ("instrument_id", "unheld"),
            ("payment_mode", "emi"),
            ("checkout_id", "different_order"),
            ("basket_fingerprint", "same_total_different_cart"),
        ):
            with self.subTest(field=field):
                # Reuse the one held application; each higher sequence remains ambiguous.
                if not self.store.rows("applications"):
                    app = self.applied()
                    self.service.dispatch(app.application_id)
                else:
                    app = self.service.inspect(self.store.rows("applications")[0]["application_id"])
                original = PaymentBinding(
                    **json.loads(self.store.rows("applications")[0]["binding_json"])
                )
                message = self.provider.event(
                    app.provider_reference, "captured", binding=replace(original, **{field: value})
                )
                self.assertEqual(self.service.receive_event(message), "binding_mismatch_reconcile")
                self.assertEqual(self.budget()["held_paise"], 120000)
                self.assertEqual(self.store.rows("benefit_obligations"), [])
        self.assertEqual(
            self.service.receive_event(self.provider.event(app.provider_reference, "failed")),
            "conflict_ignored",
        )
        self.assertEqual(self.budget()["held_paise"], 120000)

    def test_authentication_event_identity_and_stale_conflicting_evidence(self) -> None:
        app, message = self.captured()
        with self.assertRaisesRegex(ValueError, "Unauthenticated"):
            self.service.receive_event(ProviderMessage(message.payload, "invalid"))
        changed = {**message.payload, "kind": "failed"}
        with self.assertRaisesRegex(ValueError, "reused"):
            self.service.receive_event(ProviderMessage(changed, sign(changed, SYNTHETIC_EVENT_KEY)))
        later = self.provider.event(app.provider_reference, "failed")
        self.assertEqual(self.service.receive_event(later), "conflict_ignored")
        stale = {**message.payload, "event_id": "late_copy"}
        self.assertEqual(
            self.service.receive_event(ProviderMessage(stale, sign(stale, SYNTHETIC_EVENT_KEY))),
            "stale",
        )
        self.assertEqual(self.budget()["committed_paise"], 120000)
        self.assertEqual(self.service.inspect(app.application_id).benefit_state, "OWED")

    def test_refund_and_wrong_benefit_receipt_require_reconciliation(self) -> None:
        app, _ = self.captured()
        self.assertEqual(
            self.service.receive_event(
                self.provider.event(
                    app.provider_reference, "cashback_confirmed", benefit_paise=96000
                )
            ),
            "benefit_evidence_reconcile",
        )
        self.assertEqual(self.budget()["committed_paise"], 120000)
        self.assertEqual(
            self.service.receive_event(self.provider.event(app.provider_reference, "refunded")),
            "refund_reconcile",
        )
        self.assertEqual(self.store.rows("customer_usage")[0]["committed_count"], 1)

    def test_consistent_export_contains_original_history_and_replay(self) -> None:
        app, _ = self.captured()
        report = self.service.report()
        manifest = self.store.export(
            self.root / "exports",
            {"replay_report": report, "reconciliation_report": report["reconciliation_required"]},
        )
        self.assertIn("applications.json", manifest["canonical_json_sha256"])
        self.assertEqual(report["replay_totals_paise"]["liability_reserved"], 120000)
        self.assertEqual(report["replay_totals_paise"]["payment_qualified_benefit_owed"], 120000)
        self.assertTrue(
            all(row["matches_native_state"] for row in report["replay_budget_reconciliation"])
        )
        self.assertGreater(len(json.loads((self.root / "exports" / "events.json").read_text())), 4)

    def test_invented_payment_ids_cannot_double_charge_one_checkout(self) -> None:
        quote = self.service.quote(self.request)
        with self.assertRaisesRegex(ValueError, "Payment identity"):
            self.service.apply(quote.quote_id, "diwali_b_cashback", "invented_first", "key1")
        app = self.service.apply(quote.quote_id, "diwali_b_cashback", "purchase_1", "valid_key")
        with self.assertRaisesRegex(ValueError, "Payment identity"):
            self.service.apply(quote.quote_id, "diwali_a_instant", "invented_second", "key2")
        self.service.dispatch(app.application_id)
        self.assertEqual(self.provider.payment_count(), 1)
        self.assertEqual(len(self.store.rows("applications")), 1)

    def test_enabled_survivors_have_contiguous_ranks(self) -> None:
        self.service.set_campaign_enabled("diwali_b_cashback", False)
        quote = self.service.quote(self.request)
        self.assertEqual(
            [(d["offer_id"], d["rank"]) for d in quote.decisions], [("diwali_a_instant", 1)]
        )


if __name__ == "__main__":
    unittest.main()
