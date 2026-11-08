from __future__ import annotations

import argparse
import copy
import json
import platform
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

from ..demo import money
from ..io import dataset_from_dict, write_result
from ..pipeline import run_batch
from ..ranking import ExpectedSavingsScorer
from .fake_provider import SYNTHETIC_EVENT_KEY, FakeProvider
from .models import FrozenClock, canonical, digest
from .offer_service import OfferService
from .publication import prepare_bundle, verify_bundle
from .security import HmacEventVerifier
from .sqlite_store import SQLiteStore


def main() -> None:
    """Run the complete local reference flow without a live payment or cloud account."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data", type=Path, default=Path(__file__).resolve().parents[3] / "data" / "dataset.json"
    )
    parser.add_argument(
        "--bundle", type=Path, help="Verified portable serving_bundle.json exported by the notebook"
    )
    parser.add_argument("--output", type=Path, default=Path("output"))
    parser.add_argument(
        "--run-id", default=datetime.now(timezone.utc).strftime("system_%Y%m%dT%H%M%S_%fZ")
    )
    args = parser.parse_args()
    source_bytes = args.data.read_bytes()
    raw = json.loads(source_bytes)
    dataset = dataset_from_dict(raw)
    batch = run_batch(dataset, args.run_id)
    destination = args.output / args.run_id
    destination.mkdir(parents=True, exist_ok=False)
    write_result(batch, destination / "batch")
    (destination / "source_dataset.json").write_bytes(source_bytes)
    bundle = (
        verify_bundle(json.loads(args.bundle.read_text()))
        if args.bundle
        else prepare_bundle(raw, "synthetic_rules_v1")
    )
    (destination / "serving_bundle.json").write_text(canonical(bundle) + "\n")
    clock = FrozenClock(datetime(2026, 11, 5, 12, tzinfo=timezone.utc))
    store = SQLiteStore(destination / "system.sqlite")
    provider = FakeProvider(destination / "fake_provider.sqlite")
    service = OfferService(
        store,
        provider,
        clock,
        verifier=HmacEventVerifier(SYNTHETIC_EVENT_KEY),
        scorer=ExpectedSavingsScorer(),
    )
    service.prepare_snapshot(bundle, {o["offer_id"]: 500000 for o in bundle["offers"]})
    service.activate_snapshot(bundle["manifest"]["revision"])
    service.seed_wallets(raw["wallets"])
    for request in dataset.checkouts:
        service.update_checkout(
            request,
            payment_id="merchant_purchase_1001" if request.request_id == "diwali_ananya" else None,
        )
    request = next(r for r in dataset.checkouts if r.request_id == "diwali_ananya")
    quote = service.quote(request)
    application = service.apply(
        quote.quote_id, "diwali_b_cashback", "merchant_purchase_1001", "apply_request_1001"
    )
    stages = {"reserved": asdict(application)}
    service.dispatch(application.application_id)
    capture = provider.event(application.provider_reference, "captured")
    service.receive_event(capture)
    stages["payment_qualified_cashback_owed"] = asdict(service.inspect(application.application_id))
    assert stages["payment_qualified_cashback_owed"]["benefit_state"] == "OWED"
    assert service.receive_event(capture) == "duplicate"
    service.receive_event(
        provider.event(application.provider_reference, "cashback_confirmed", benefit_paise=120000)
    )
    stages["cashback_confirmed"] = asdict(service.inspect(application.application_id))
    assert (
        service.apply(
            quote.quote_id, "diwali_b_cashback", "merchant_purchase_1001", "apply_request_1001"
        ).application_id
        == application.application_id
    )
    duplicate_payment_refused = False
    try:
        service.apply(
            quote.quote_id, "diwali_a_instant", "merchant_purchase_1001", "different_request_key"
        )
    except ValueError:
        duplicate_payment_refused = True
    assert duplicate_payment_refused
    # A separate payment demonstrates a definitive failure and release.
    failed_request = replace(request, request_id="diwali_failed_order")
    service.update_checkout(failed_request, payment_id="merchant_purchase_1002")
    instant_quote = service.quote(failed_request)
    failed = service.apply(
        instant_quote.quote_id, "diwali_a_instant", "merchant_purchase_1002", "apply_request_1002"
    )
    service.dispatch(failed.application_id)
    service.receive_event(provider.event(failed.provider_reference, "failed"))
    # A second customer demonstrates uncertain delivery and the original-reference lookup.
    wallet = copy.deepcopy(raw["wallets"][0])
    wallet["user_id"] = "reconciliation_customer"
    for instrument in wallet["instruments"]:
        instrument["instrument_id"] = "reconcile_" + instrument["instrument_id"]
    service.seed_wallets([wallet])
    uncertain_request = replace(request, user_id=wallet["user_id"], request_id="uncertain_checkout")
    service.update_checkout(uncertain_request, payment_id="merchant_purchase_1003")
    uncertain_quote = service.quote(uncertain_request)
    uncertain = service.apply(
        uncertain_quote.quote_id,
        "diwali_b_cashback",
        "merchant_purchase_1003",
        "apply_request_1003",
    )
    provider.timeout_after_accept = True
    service.dispatch(uncertain.application_id)
    unknown = asdict(service.inspect(uncertain.application_id))
    assert unknown["state"] == "UNKNOWN"
    no_evidence = service.reconcile(uncertain.application_id)
    provider.event(uncertain.provider_reference, "captured")
    recovered = service.reconcile(uncertain.application_id)
    report = service.report()
    assert all(row["conserved"] for row in report["conservation"])
    summary = {
        "run_id": args.run_id,
        "python": platform.python_version(),
        "scope": "synthetic local SQLite and fake provider; cloud integrations not executed",
        "publication_manifest": bundle["manifest"],
        "batch_decisions": len(batch.decisions),
        "batch_ranked": len(batch.slate),
        "quoted_offer_expected_paise": {
            d["offer_id"]: d["expected_benefit_paise"] for d in quote.decisions
        },
        "primary_application_stages": stages,
        "duplicate_payment_refused": duplicate_payment_refused,
        "definitive_failure": asdict(service.inspect(failed.application_id)),
        "uncertain_after_dispatch": unknown,
        "lookup_without_evidence": no_evidence,
        "lookup_after_capture": recovered,
        "recovered_application": asdict(service.inspect(uncertain.application_id)),
        "funding_conserved": all(row["conserved"] for row in report["conservation"]),
    }
    (destination / "scenario_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    store.export(
        destination / "exports",
        {"replay_report": report, "reconciliation_report": report["reconciliation_required"]},
    )
    (destination / "run_manifest.json").write_text(
        json.dumps(
            {
                "run_id": args.run_id,
                "source_canonical_sha256": digest(raw),
                "bundle_content_sha256": bundle["manifest"]["content_sha256"],
                "python": platform.python_version(),
                "completed": True,
            },
            indent=2,
        )
        + "\n"
    )
    print(
        "Synthetic LOCAL reference workflow; SQLite + fake provider. No real payment or Databricks run."
    )
    print(
        f"Publication: {bundle['manifest']['source_count']} source / {bundle['manifest']['approved_count']} approved / {bundle['manifest']['quarantine_count']} quarantined"
    )
    for decision in quote.decisions:
        print(
            f"Quote {decision['offer_id']}: nominal={money(decision['nominal_benefit_paise'])}, expected={money(decision['expected_benefit_paise'])}"
        )
    print(
        "Reserved ₹1200.00 maximum liability; capture -> cashback OWED; separate receipt -> FULFILLED"
    )
    print(
        "Exact retry reused application; changed request key could not award the same payment twice"
    )
    print(
        "Definitive failure -> RELEASED; uncertain dispatch stayed HELD until original-reference reconciliation -> OWED"
    )
    print(
        f"Budget conservation: {summary['funding_conserved']}; batch decisions={len(batch.decisions)}, ranked={len(batch.slate)}"
    )
    print(f"Durable database, snapshots, events and exports: {destination.resolve()}")


if __name__ == "__main__":
    main()
