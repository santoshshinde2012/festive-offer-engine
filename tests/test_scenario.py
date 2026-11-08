from __future__ import annotations

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, localcontext
from pathlib import Path

from festive_offers.io import dataset_from_dict, load_dataset, write_result
from festive_offers.models import LineItem
from festive_offers.pipeline import run_batch
from festive_offers.ranking import ExpectedSavingsScorer, rank_checkout
from festive_offers.validation import validate_checkout

FIXTURE = Path(__file__).resolve().parents[1] / "data" / "dataset.json"


class ScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = load_dataset(FIXTURE)
        self.request = self.data.checkouts[0]
        self.offer = self.data.offers[0]
        self.wallet = self.data.wallets["ananya"]

    def decisions(self, offers=None, request=None, instruments=None, assumptions=None):
        return rank_checkout(
            request or self.request,
            offers if offers is not None else (self.offer,),
            instruments if instruments is not None else self.wallet,
            assumptions if assumptions is not None else self.data.assumptions,
            "test_run",
        )

    def test_article_numbers_and_campaign_reuse(self) -> None:
        result = run_batch(self.data, "article_fixture")
        self.assertEqual(len(result.decisions), 24)
        self.assertEqual(len(result.slate), 4)
        self.assertEqual(
            [
                (d.offer_id, d.nominal_benefit_paise, d.expected_benefit_paise)
                for d in result.slate
                if d.request_id == "diwali_ananya"
            ],
            [("diwali_b_cashback", 120000, 96000), ("diwali_a_instant", 75000, 75000)],
        )
        self.assertEqual(
            [
                (d.offer_id, d.expected_benefit_paise)
                for d in result.slate
                if d.request_id == "christmas_ananya"
            ],
            [("christmas_b_cashback", 120000), ("christmas_a_instant", 100000)],
        )
        self.assertEqual(result.metrics["requests_with_offer"], 2)
        self.assertEqual(result.metrics["top_issuer_counts"], {"Bank B": 2})

    def test_alternate_scorer_changes_order_without_changing_eligibility(self) -> None:
        class ConservativeCashbackScorer:
            """Demonstrate another policy through the existing narrow scoring port."""

            def __bool__(self) -> bool:
                # A valid implementation must not be replaced merely for being falsy.
                return False

            def expected_paise(self, nominal_paise: int, probability: Decimal) -> int:
                adjusted = probability if probability == 1 else min(probability, Decimal("0.25"))
                numerator, denominator = adjusted.as_integer_ratio()
                return nominal_paise * numerator // denominator

        baseline = run_batch(self.data, "alternate_policy")
        alternative = run_batch(self.data, "alternate_policy", ConservativeCashbackScorer())
        self.assertEqual(
            [
                (decision.offer_id, decision.eligible, decision.rejection_reason)
                for decision in alternative.decisions
            ],
            [
                (decision.offer_id, decision.eligible, decision.rejection_reason)
                for decision in baseline.decisions
            ],
        )
        self.assertEqual(
            [
                (decision.request_id, decision.offer_id)
                for decision in alternative.slate
                if decision.rank == 1
            ],
            [("christmas_ananya", "christmas_a_instant"), ("diwali_ananya", "diwali_a_instant")],
        )
        cashback = next(
            decision for decision in alternative.slate if decision.offer_id == "diwali_b_cashback"
        )
        self.assertEqual(
            (cashback.nominal_benefit_paise, cashback.expected_benefit_paise), (120000, 30000)
        )
        self.assertEqual(alternative.metrics["top_issuer_counts"], {"Bank A": 2})

    def test_invalid_scorer_cannot_violate_expected_money_contract(self) -> None:
        class InvalidScorer:
            def __init__(self, result: object) -> None:
                self.result = result

            def expected_paise(self, nominal_paise: int, probability: Decimal) -> object:
                return self.result

        for malformed in (1.5, True, -1, 1_000_000_000):
            with self.subTest(result=malformed):
                with self.assertRaisesRegex(ValueError, "BenefitScorer"):
                    run_batch(self.data, "invalid_scorer", InvalidScorer(malformed))

    def test_empty_or_unknown_wallet_never_borrows_card(self) -> None:
        self.assertEqual(
            self.decisions(instruments=())[0].rejection_reason, "no_eligible_instrument"
        )
        result = run_batch(replace(self.data, wallets={}), "empty")
        self.assertEqual(result.slate, ())

    def test_higher_banner_for_unheld_card_refused(self) -> None:
        self.assertEqual(
            self.decisions(offers=(self.data.offers[2],))[0].rejection_reason,
            "no_eligible_instrument",
        )

    def test_product_network_and_mode_are_exact(self) -> None:
        for key, value in (
            ("product", "bank_a_cobrand"),
            ("network", "mastercard"),
            ("payment_mode", "emi"),
        ):
            with self.subTest(key=key):
                instrument = replace(self.wallet[0], **{key: value})
                self.assertFalse(self.decisions(instruments=(instrument,))[0].eligible)
        self.assertFalse(
            self.decisions(instruments=(replace(self.wallet[0], verified=False),))[0].eligible
        )

    def test_missing_contract_terms_fail_closed(self) -> None:
        for update, expected in (
            ({"cap_paise": None}, "unknown_cap"),
            ({"min_eligible_spend_paise": None}, "unknown_minimum"),
            ({"instrument_rules": ()}, "unknown_instrument_rules"),
            ({"payout": "points"}, "unknown_payout"),
            ({"cap_paise": -1}, "invalid_contract"),
            ({"rate": Decimal("NaN")}, "invalid_contract"),
        ):
            with self.subTest(update=update):
                self.assertEqual(
                    self.decisions(offers=(replace(self.offer, **update),))[0].rejection_reason,
                    expected,
                )

    def test_unknown_probability_refuses_cashback(self) -> None:
        for assumptions in (
            {},
            {"delayed_cashback": Decimal("1.2")},
            {"delayed_cashback": Decimal("NaN")},
        ):
            self.assertEqual(
                self.decisions(offers=(self.data.offers[1],), assumptions=assumptions)[
                    0
                ].rejection_reason,
                "unknown_realization_probability",
            )

    def test_start_inclusive_end_exclusive(self) -> None:
        self.assertTrue(
            self.decisions(request=replace(self.request, at=self.offer.starts_at))[0].eligible
        )
        self.assertFalse(
            self.decisions(
                request=replace(self.request, at=self.offer.starts_at - timedelta(microseconds=1))
            )[0].eligible
        )
        self.assertFalse(
            self.decisions(request=replace(self.request, at=self.offer.ends_at))[0].eligible
        )

    def test_merchant_mismatch(self) -> None:
        self.assertEqual(
            self.decisions(request=replace(self.request, merchant="other_shop"))[
                0
            ].rejection_reason,
            "merchant_mismatch",
        )

    def test_minimum_and_rate_use_only_eligible_net_lines(self) -> None:
        items = (
            LineItem("electronics", "example_brand", 400000),
            LineItem("electronics", "excluded_brand", 400000),
            LineItem("grocery", "example_brand", 200000),
        )
        decision = self.decisions(request=replace(self.request, items=items))[0]
        self.assertEqual(decision.eligible_spend_paise, 400000)
        self.assertEqual(decision.rejection_reason, "below_minimum")
        offer = replace(self.offer, min_eligible_spend_paise=400000)
        decision = self.decisions(offers=(offer,), request=replace(self.request, items=items))[0]
        self.assertEqual(decision.nominal_benefit_paise, 40000)

    def test_no_eligible_spend(self) -> None:
        request = replace(self.request, items=(LineItem("grocery", "example_brand", 1000000),))
        self.assertEqual(self.decisions(request=request)[0].rejection_reason, "no_eligible_spend")

    def test_integer_paise_rounding(self) -> None:
        request = replace(self.request, items=(LineItem("electronics", "example_brand", 10001),))
        offer = replace(self.offer, min_eligible_spend_paise=0, cap_paise=100000)
        self.assertEqual(
            self.decisions(offers=(offer,), request=request)[0].nominal_benefit_paise, 1000
        )

    def test_money_is_exact_independent_of_decimal_context(self) -> None:
        rate = Decimal("0.099999999999999999999999999999")
        q = Decimal("0.799999999999999999999999999999")
        offer = replace(self.offer, rate=rate, cap_paise=1000000)
        with localcontext() as context:
            context.prec = 3
            self.assertEqual(self.decisions(offers=(offer,))[0].nominal_benefit_paise, 99999)
            self.assertEqual(ExpectedSavingsScorer().expected_paise(100000, q), 79999)

    def test_stable_ties_and_one_row_per_offer(self) -> None:
        offers = (replace(self.offer, offer_id="z"), replace(self.offer, offer_id="a"))
        wallet = (*self.wallet, replace(self.wallet[0], instrument_id="a_card"))
        decisions = self.decisions(offers=offers, instruments=wallet)
        self.assertEqual(
            [(d.offer_id, d.rank, d.instrument_id) for d in decisions],
            [("z", 2, "a_card"), ("a", 1, "a_card")],
        )
        self.assertEqual(len(decisions), 2)

    def test_empty_output_serializes_with_csv_headers(self) -> None:
        result = run_batch(replace(self.data, wallets={}), "empty")
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "empty"
            write_result(result, destination)
            self.assertEqual(json.loads((destination / "slate.json").read_text()), [])
            self.assertIn("expected_benefit_paise", (destination / "slate.csv").read_text())
            with self.assertRaises(FileExistsError):
                write_result(result, destination)

    def test_malformed_money_stops_batch(self) -> None:
        raw = json.loads(FIXTURE.read_text())
        for value in (12.3, True, "100"):
            changed = copy.deepcopy(raw)
            changed["checkouts"][0]["items"][0]["net_amount_paise"] = value
            with self.assertRaises(ValueError):
                dataset_from_dict(changed)

    def test_unsupported_constraints_are_never_silently_ignored(self) -> None:
        raw = json.loads(FIXTURE.read_text())
        changed = copy.deepcopy(raw)
        changed["offers"][0]["category_caps_paise"] = {"electronics": 1000}
        with self.assertRaisesRegex(ValueError, "category_caps_paise"):
            dataset_from_dict(changed)
        changed = copy.deepcopy(raw)
        changed["offers"][0]["instrument_rules"][0]["excluded_product_variant"] = "corporate"
        with self.assertRaisesRegex(ValueError, "excluded_product_variant"):
            dataset_from_dict(changed)
        changed = copy.deepcopy(raw)
        changed["checkouts"][0]["items"][0]["restricted_subcategory"] = "phones"
        with self.assertRaisesRegex(ValueError, "restricted_subcategory"):
            dataset_from_dict(changed)

    def test_duplicate_ids_and_bounded_batch_fail(self) -> None:
        for data in (
            replace(self.data, offers=(self.offer, self.offer)),
            replace(self.data, checkouts=self.data.checkouts * 34),
            replace(self.data, offers=self.data.offers * 13),
            replace(self.data, wallets={"ananya": self.wallet * 11}),
        ):
            with self.assertRaises(ValueError):
                run_batch(data, "invalid")

    def test_naive_time_and_negative_lines_stop_batch(self) -> None:
        for request in (
            replace(self.request, at=self.request.at.replace(tzinfo=None)),
            replace(self.request, items=(LineItem("electronics", "brand", -1),)),
        ):
            with self.assertRaises(ValueError):
                run_batch(replace(self.data, checkouts=(request,)), "invalid")

    def test_typed_checkout_preserves_text_validation_at_input_boundary(self) -> None:
        for field in ("request_id", "user_id", "merchant", "currency"):
            for value in ("", 123):
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(ValueError, "nonempty string"):
                        validate_checkout(replace(self.request, **{field: value}))
        for field in ("category", "brand"):
            for value in ("", None):
                with self.subTest(field=field, value=value):
                    item = replace(self.request.items[0], **{field: value})
                    with self.assertRaisesRegex(ValueError, "nonempty string"):
                        validate_checkout(replace(self.request, items=(item,)))


if __name__ == "__main__":
    unittest.main()
