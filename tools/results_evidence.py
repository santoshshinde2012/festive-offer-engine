"""Pure integrity and replay validation for an already-produced local run."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Any

from festive_offers.online.models import Application
from festive_offers.online.reporting_service import ReportingService
from festive_offers.pipeline import run_batch
from festive_offers.serialization import dataset_from_dict

TABLES = (
    "snapshots",
    "active_snapshot",
    "campaign_control",
    "budgets",
    "wallets",
    "checkouts",
    "quotes",
    "applications",
    "customer_usage",
    "outbox",
    "inbox",
    "benefit_obligations",
    "events",
)
REPORTS = ("replay_report", "reconciliation_report")


def canonical_sha256(value: Any) -> str:
    """Match the documented canonical JSON export hash independently of file whitespace."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def parse_test_result(log: str) -> dict[str, Any]:
    """Count successful test execution separately from discovered, skipped tests."""
    matches = re.findall(
        r"^Ran (\d+) tests? in ([0-9]+(?:\.[0-9]+)?)s[ \t]*\r?\n"
        r"(?:[ \t]*\r?\n)*[ \t]*OK([^\r\n]*)\r?$",
        log,
        flags=re.MULTILINE,
    )
    if len(matches) != 1 or re.search(r"(?:^|\n)FAILED(?:\s|\()", log):
        raise ValueError("The captured log does not contain exactly one successful test run")
    count, duration, suffix = matches[0]
    discovered = int(count)
    if discovered < 1:
        raise ValueError("The captured command ran no application tests")
    skipped = 0
    if suffix.strip():
        skipped_match = re.fullmatch(r"\(skipped=(\d+)\)", suffix.strip())
        if skipped_match is None:
            raise ValueError("Unsupported successful unittest summary suffix")
        skipped = int(skipped_match[1])
        if skipped > discovered:
            raise ValueError("Skipped test count exceeds discovered tests")
    result = {"count": discovered - skipped, "duration_seconds": float(duration), "status": "OK"}
    # Preserve the original envelope exactly for retained non-skipped evidence.
    if skipped:
        result.update({"discovered": discovered, "skipped": skipped})
    return result


def format_test_result(result: Mapping[str, Any]) -> str:
    """Render both historical and skipped-run envelopes without calling skips passes."""
    text = f"{result['count']} passed"
    if result.get("skipped"):
        text += f", {result['skipped']} skipped ({result['discovered']} discovered)"
    return text


@dataclass(frozen=True)
class ExportReports:
    """Adapt immutable JSON arrays to the narrow report snapshot port, without a database."""

    artifacts: Mapping[str, Any]

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        return {
            name: copy.deepcopy(self.artifacts[f"exports/{name}.json"])
            for name in (
                "budgets",
                "events",
                "applications",
                "benefit_obligations",
            )
        }


@dataclass(frozen=True)
class ExportReportUnit:
    """Expose only the read snapshot required to replay saved exports."""

    reports: ExportReports


@dataclass(frozen=True)
class ExportReportUnits:
    """Supply a read-only exported snapshot instead of opening or creating SQLite files."""

    artifacts: Mapping[str, Any]

    @contextmanager
    def unit_of_work(self) -> Iterator[ExportReportUnit]:
        yield ExportReportUnit(ExportReports(self.artifacts))


def _validate_scenario(summary: Mapping[str, Any], artifacts: Mapping[str, Any]) -> None:
    """Tie every displayed lifecycle checkpoint to identity, final rows and ordered events."""
    applications = {row["application_id"]: row for row in artifacts["exports/applications.json"]}
    events = artifacts["exports/events.json"]
    sequences = [event["event_sequence"] for event in events]
    if len(sequences) != len(set(sequences)) or any(
        type(value) is not int or value < 1 for value in sequences
    ):
        raise ValueError("The journal has invalid event ordering identities")
    view_fields = tuple(Application.__dataclass_fields__)
    state_fields = ("state", "payment_state", "benefit_state", "budget_state")
    identity_fields = tuple(field for field in view_fields if field not in state_fields)

    def final_row(view: Mapping[str, Any]) -> Mapping[str, Any]:
        native = applications.get(view["application_id"])
        if native is None or any(native[field] != view[field] for field in view_fields):
            raise ValueError("A displayed final application differs from its durable record")
        return native

    def checkpoint(view: Mapping[str, Any], expected: tuple[str, str, str, str]) -> None:
        if tuple(view[field] for field in state_fields) != expected:
            raise ValueError(
                "A displayed lifecycle checkpoint has invalid payment/benefit/budget state"
            )

    def journal(application_id: str, kind: str, **payload_fields: Any) -> Mapping[str, Any]:
        matched = [
            event
            for event in events
            if event["application_id"] == application_id
            and event["kind"] == kind
            and all(
                json.loads(event["payload_json"]).get(field) == value
                for field, value in payload_fields.items()
            )
        ]
        if len(matched) != 1:
            raise ValueError(f"The displayed lifecycle is not backed by one journal event: {kind}")
        return matched[0]

    stages = summary["primary_application_stages"]
    if set(stages) != {"reserved", "payment_qualified_cashback_owed", "cashback_confirmed"}:
        raise ValueError("The primary lifecycle checkpoint set is incomplete")
    reserved, qualified, delivered = (
        stages[name]
        for name in ("reserved", "payment_qualified_cashback_owed", "cashback_confirmed")
    )
    for view in (qualified, delivered):
        if any(view[field] != reserved[field] for field in identity_fields):
            raise ValueError("The primary lifecycle checkpoints refer to different payments")
    checkpoint(reserved, ("HELD", "NOT_SENT", "NONE", "HELD"))
    checkpoint(qualified, ("OWED", "CAPTURED", "OWED", "COMMITTED"))
    checkpoint(delivered, ("FULFILLED", "CAPTURED", "FULFILLED", "COMMITTED"))
    primary = final_row(delivered)
    reserve_event = journal(
        primary["application_id"], "liability_reserved", amount_paise=primary["nominal_paise"]
    )
    claim_event = journal(
        primary["application_id"],
        "dispatch_claimed",
        provider_reference=primary["provider_reference"],
    )
    qualify_event = journal(
        primary["application_id"],
        "payment_qualified_benefit_owed",
        amount_paise=primary["nominal_paise"],
    )
    fulfill_event = journal(
        primary["application_id"], "benefit_fulfilled", amount_paise=primary["nominal_paise"]
    )
    if (
        not reserve_event["event_sequence"]
        < claim_event["event_sequence"]
        < qualify_event["event_sequence"]
        < fulfill_event["event_sequence"]
    ):
        raise ValueError("The primary lifecycle journal is out of order")
    quotes = {row["quote_id"]: row for row in artifacts["exports/quotes.json"]}
    primary_quote = quotes.get(primary["quote_id"])
    if primary_quote is None or summary["quoted_offer_expected_paise"] != {
        row["offer_id"]: row["expected_benefit_paise"]
        for row in json.loads(primary_quote["decisions_json"])
    }:
        raise ValueError("Displayed quote values differ from the saved quote")

    failed = final_row(summary["definitive_failure"])
    checkpoint(failed, ("RELEASED", "FAILED", "NONE", "RELEASED"))
    failed_reserve = journal(
        failed["application_id"], "liability_reserved", amount_paise=failed["nominal_paise"]
    )
    released = journal(
        failed["application_id"],
        "liability_released",
        amount_paise=failed["nominal_paise"],
        reason="provider_definitive_failure",
    )
    if failed_reserve["event_sequence"] >= released["event_sequence"]:
        raise ValueError("The definitive failure release precedes its reservation")
    uncertain = summary["uncertain_after_dispatch"]
    recovered = final_row(summary["recovered_application"])
    checkpoint(uncertain, ("UNKNOWN", "UNKNOWN", "NONE", "HELD"))
    checkpoint(recovered, ("OWED", "CAPTURED", "OWED", "COMMITTED"))
    if any(uncertain[field] != recovered[field] for field in identity_fields):
        raise ValueError("Uncertain and recovered checkpoints refer to different payments")
    timed_out = journal(
        recovered["application_id"], "dispatch_result", state="UNKNOWN", reason="TimeoutError"
    )
    recovered_capture = journal(
        recovered["application_id"],
        "payment_qualified_benefit_owed",
        amount_paise=recovered["nominal_paise"],
    )
    if timed_out["event_sequence"] >= recovered_capture["event_sequence"]:
        raise ValueError("The recovered capture does not follow the uncertain dispatch")
    if (
        summary["duplicate_payment_refused"] is not True
        or summary["lookup_without_evidence"] != "no_definitive_provider_evidence"
        or summary["lookup_after_capture"] != "applied"
    ):
        raise ValueError("The displayed duplicate/reconciliation checkpoint is invalid")


def validate_run(artifacts: Mapping[str, Any], expected_run_id: str) -> dict[str, Any]:
    """Check completion, exact publication identity, every export hash and replay accounting."""
    run = artifacts["run_manifest.json"]
    summary = artifacts["scenario_summary.json"]
    if (
        run.get("completed") is not True
        or run.get("run_id") != expected_run_id
        or summary.get("run_id") != expected_run_id
    ):
        raise ValueError("Run completion marker or run identity is invalid")
    source = artifacts["source_dataset.json"]
    bundle = artifacts["serving_bundle.json"]
    manifest = bundle["manifest"]
    body = {name: bundle[name] for name in ("offers", "assumptions", "quarantined")}
    if canonical_sha256(source) != run["source_canonical_sha256"]:
        raise ValueError("The source snapshot does not match its run hash")
    if (
        canonical_sha256(body) != manifest["content_sha256"]
        or run["bundle_content_sha256"] != manifest["content_sha256"]
    ):
        raise ValueError("The serving bundle does not match its publication hash")
    if summary["publication_manifest"] != manifest:
        raise ValueError("The scenario summary refers to a different publication")
    if (
        manifest["approved_count"] != len(bundle["offers"])
        or manifest["quarantine_count"] != len(bundle["quarantined"])
        or manifest["source_count"] != len(source["offers"])
        or manifest["source_count"] != manifest["approved_count"] + manifest["quarantine_count"]
    ):
        raise ValueError("The source/approval/quarantine manifest is incomplete")
    rejected = {row["source_index"]: row["source_record"] for row in bundle["quarantined"]}
    if len(rejected) != len(bundle["quarantined"]) or any(
        type(index) is not int or not 0 <= index < manifest["source_count"] for index in rejected
    ):
        raise ValueError("Invalid quarantine source identity")
    approved = iter(bundle["offers"])
    reconstructed = [
        rejected[index] if index in rejected else next(approved)
        for index in range(manifest["source_count"])
    ]
    if reconstructed != source["offers"] or bundle["assumptions"] != source["assumptions"]:
        raise ValueError("Published rules do not exactly reconstruct the source snapshot")

    export_manifest = artifacts["exports/export_manifest.json"]
    expected_files = {f"{name}.json" for name in TABLES + REPORTS}
    hashes = export_manifest["canonical_json_sha256"]
    if export_manifest.get("schema_version") != 1 or set(hashes) != expected_files:
        raise ValueError("The export manifest does not contain the complete table/report set")
    for name, expected_hash in hashes.items():
        if canonical_sha256(artifacts[f"exports/{name}"]) != expected_hash:
            raise ValueError(f"Export hash mismatch: {name}")
    active = artifacts["exports/active_snapshot.json"]
    snapshots = artifacts["exports/snapshots.json"]
    if len(active) != 1 or active[0]["revision"] != manifest["revision"]:
        raise ValueError("The native active pointer refers to a different revision")
    native = [row for row in snapshots if row["revision"] == manifest["revision"]]
    if len(native) != 1 or json.loads(native[0]["bundle_json"]) != bundle:
        raise ValueError("The native snapshot differs from the exact serving bundle")

    replay = ReportingService(ExportReportUnits(artifacts)).report()
    if (
        replay != artifacts["exports/replay_report.json"]
        or replay["reconciliation_required"] != artifacts["exports/reconciliation_report.json"]
    ):
        raise ValueError("Saved replay/reconciliation reports differ from the exported records")
    budgets = artifacts["exports/budgets.json"]
    for budget in budgets:
        values = [
            budget[key]
            for key in ("funding_paise", "available_paise", "held_paise", "committed_paise")
        ]
        if any(type(value) is not int or value < 0 for value in values) or values[0] != sum(
            values[1:]
        ):
            raise ValueError("Native budget conservation is invalid")
    if not all(row["conserved"] for row in replay["conservation"]) or not all(
        row["matches_native_state"] for row in replay["replay_budget_reconciliation"]
    ):
        raise ValueError("Event replay does not reconcile to native funding buckets")
    applications = artifacts["exports/applications.json"]
    obligations = artifacts["exports/benefit_obligations.json"]
    by_id = {row["application_id"]: row for row in applications}
    if len(by_id) != len(applications):
        raise ValueError("Duplicate application identity in exported records")
    obligation_ids: set[str] = set()
    for obligation in obligations:
        app_id = obligation["application_id"]
        if app_id in obligation_ids or app_id not in by_id:
            raise ValueError("Duplicate or unknown benefit obligation identity")
        obligation_ids.add(app_id)
        app = by_id[app_id]
        if (
            app["payment_state"] != "CAPTURED"
            or app["budget_state"] != "COMMITTED"
            or app["benefit_state"] != obligation["state"]
            or app["nominal_paise"] != obligation["amount_paise"]
        ):
            raise ValueError("Benefit delivery state does not match its qualified application")
    if {
        row["application_id"]
        for row in applications
        if row["benefit_state"] in {"OWED", "FULFILLED"}
    } != obligation_ids:
        raise ValueError("A qualified application is missing its benefit obligation")
    _validate_scenario(summary, artifacts)
    if summary.get("funding_conserved") is not True:
        raise ValueError("The scenario did not confirm its funding invariant")
    if summary["batch_decisions"] != len(artifacts["batch/decisions.json"]) or summary[
        "batch_ranked"
    ] != len(artifacts["batch/slate.json"]):
        raise ValueError("The batch counts do not match the saved decision arrays")
    raw_batch = run_batch(dataset_from_dict(source), expected_run_id)
    if (
        raw_batch.metrics != artifacts["batch/metrics.json"]
        or [asdict(row) for row in raw_batch.decisions] != artifacts["batch/decisions.json"]
        or [asdict(row) for row in raw_batch.slate] != artifacts["batch/slate.json"]
    ):
        raise ValueError("Displayed batch metrics or decision values differ from the source replay")
    approved_batch = run_batch(
        dataset_from_dict(
            {
                "offers": bundle["offers"],
                "assumptions": bundle["assumptions"],
                "wallets": source["wallets"],
                "checkouts": source["checkouts"],
            }
        ),
        "approved_evidence_comparison",
    )
    approved_business = [
        {key: value for key, value in asdict(row).items() if key != "run_id"}
        for row in approved_batch.slate
    ]
    raw_business = [
        {key: value for key, value in row.items() if key != "run_id"}
        for row in artifacts["batch/slate.json"]
    ]
    if approved_business != raw_business:
        raise ValueError("Approved-only ranking does not match the raw fixture's eligible slate")
    owed = [row for row in obligations if row["state"] == "OWED"]
    fulfilled = [row for row in obligations if row["state"] == "FULFILLED"]
    return {
        "table_count": len(TABLES),
        "report_count": len(REPORTS),
        "canonical_hashes_verified": len(hashes),
        "publication_revision": manifest["revision"],
        "source_count": manifest["source_count"],
        "approved_count": manifest["approved_count"],
        "quarantine_count": manifest["quarantine_count"],
        "budget_conserved": True,
        "replay_matches_native": True,
        "application_count": len(applications),
        "owed_count": len(owed),
        "owed_total_paise": sum(row["amount_paise"] for row in owed),
        "fulfilled_count": len(fulfilled),
        "fulfilled_total_paise": sum(row["amount_paise"] for row in fulfilled),
        "reconciliation_required_count": len(replay["reconciliation_required"]),
        "batch_decision_count": summary["batch_decisions"],
        "batch_ranked_count": summary["batch_ranked"],
        "approved_decision_count": len(approved_batch.decisions),
        "approved_ranked_count": len(approved_batch.slate),
    }
