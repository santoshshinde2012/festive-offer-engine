"""Render the verified local evidence as a readable report and snapshot views."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .evidence_files import load_report_data
from .evidence_models import ReportData
from .results_evidence import format_test_result


@dataclass(frozen=True)
class ResultSection:
    """Keep each screenshot bound to one report section and its raw source files."""

    slug: str
    title: str
    body: str
    sources: tuple[str, ...]


def _safe(value: Any) -> str:
    return html.escape(str(value))


def _money(paise: int) -> str:
    sign = "−" if paise < 0 else ""
    rupees, remainder = divmod(abs(paise), 100)
    return f"{sign}₹{rupees:,}.{remainder:02d}"


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    head = "".join(f"<th>{_safe(value)}</th>" for value in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_safe(value)}</td>" for value in row) + "</tr>" for row in rows
    )
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _cards(values: Sequence[tuple[str, Any]]) -> str:
    return (
        '<div class="cards">'
        + "".join(
            f'<div class="card"><span>{_safe(label)}</span><strong>{_safe(value)}</strong></div>'
            for label, value in values
        )
        + "</div>"
    )


def pure_sections(data: ReportData) -> tuple[ResultSection, ...]:
    """Build section content solely from one already-loaded evidence context."""
    manifest, summary, bundle = data.manifest, data.summary, data.bundle
    replay, slate, metrics = data.replay, data.slate, data.metrics
    test = manifest["test_result"]
    log = data.log
    # An exact excerpt of the retained transcript, not invented terminal output.
    marker = log.find("\nRan ")
    excerpt = log[marker + 1 :] if marker >= 0 else log
    check = manifest["verification"]
    publication = bundle["manifest"]
    source_count = publication["source_count"]
    approved = publication["approved_count"]
    tests = (
        _cards(
            [
                ("Application tests", format_test_result(test)),
                ("Process exit", manifest["exit_code"]),
                ("Python", manifest["runtime"]["python"]),
                ("Run completed", "yes"),
            ]
        )
        + "<p>The transcript below is copied from this run. The full test names remain in <code>evidence/run.log</code>.</p>"
    )
    tests += f"<pre>{_safe(excerpt.rstrip())}</pre>"
    pub = _cards(
        [
            ("Source offers", source_count),
            ("Approved", approved),
            ("Quarantined", publication["quarantine_count"]),
        ]
    )
    pub += _table(
        ["Quarantined offer", "Reason"],
        [(row["source_record"]["offer_id"], row["reason"]) for row in bundle["quarantined"]],
    )
    pub += f'<p>Serving revision: <code>{_safe(publication["revision"])}</code>. Exact bundle count/hash and retained application snapshot match.</p><p class="digest">Bundle SHA-256: {_safe(publication["content_sha256"])}</p>'
    ranks = _table(
        ["Request", "Rank", "Offer", "Nominal benefit", "Expected saving", "Payout"],
        [
            (
                row["request_id"],
                row["rank"],
                row["offer_id"],
                _money(row["nominal_benefit_paise"]),
                _money(row["expected_benefit_paise"]),
                row["payout"],
            )
            for row in slate
        ],
    )
    bank_b = next(row for row in slate if row["offer_id"] == "diwali_b_cashback")
    bank_a = next(row for row in slate if row["offer_id"] == "diwali_a_instant")
    ranks += f"<p>Bank B uses an assumed {bank_b['realization_probability']} realization chance. Comparing {_money(bank_b['expected_benefit_paise'])} does not reduce its {_money(bank_b['nominal_benefit_paise'])} reservation. Bank A saves {_money(bank_a['nominal_benefit_paise'])} immediately.</p>"
    ranks += f"<p>The raw compatibility batch checks all {source_count} rows: {summary['batch_decisions']} decisions and {summary['batch_ranked']} ranked rows. The locally verified approved-only kernel evaluates {approved} approved rows: {check['approved_decision_count']} decisions and {check['approved_ranked_count']} ranked rows across {metrics['request_count']} fixture checkouts. The Databricks workspace path is supplied; it has not been run here.</p>"
    stages = summary["primary_application_stages"]
    lifecycle = _table(
        ["Recorded checkpoint", "Application", "Payment", "Benefit", "Funding", "Liability"],
        [
            (
                label.replace("_", " "),
                row["state"],
                row["payment_state"],
                row["benefit_state"],
                row["budget_state"],
                _money(row["nominal_paise"]),
            )
            for label, row in stages.items()
        ],
    )
    application = next(iter(stages.values()))
    lifecycle += f'<p>These checkpoints were captured by the scenario and retained in <code>scenario_summary.json</code>; they are not separate live checkout screenshots.</p><p class="digest">Same application: {_safe(application["application_id"])}</p>'
    lifecycle += "<p>A full capture creates cashback owed. A separate matching benefit receipt confirms fulfillment. Fulfilled spending remains committed.</p>"
    failure_rows = []
    for label, key in [
        ("Definitive failure", "definitive_failure"),
        ("Timeout after provider accepted", "uncertain_after_dispatch"),
        ("Original-reference recovery", "recovered_application"),
    ]:
        row = summary[key]
        failure_rows.append(
            (
                label,
                row["state"],
                row["payment_state"],
                row["benefit_state"],
                row["budget_state"],
                _money(row["nominal_paise"]),
            )
        )
    failures = _table(
        ["Case", "Application", "Payment", "Benefit", "Funding", "Liability"], failure_rows
    )
    failures += f"<p>Lookup before evidence: <code>{_safe(summary['lookup_without_evidence'])}</code>. Lookup after capture: <code>{_safe(summary['lookup_after_capture'])}</code>.</p>"
    failures += f"<p>Second application for the same payment refused: <strong>{_safe(summary['duplicate_payment_refused'])}</strong>. The scenario also checks that an exact retry returns the same application and duplicate capture has no repeated effect; the transcript completes only after those checks pass.</p>"
    failures += f"<p>The recovered purchase still has {_money(summary['recovered_application']['nominal_paise'])} cashback owed. Payment recovery does not confirm cashback payout.</p>"
    ledger = _table(
        ["Campaign", "Funding", "Available", "Held", "Committed", "Conserved"],
        [
            (
                row["offer_id"],
                _money(row["funding_paise"]),
                _money(row["available_paise"]),
                _money(row["held_paise"]),
                _money(row["committed_paise"]),
                row["conserved"],
            )
            for row in replay["conservation"]
        ],
    )
    totals = replay["replay_totals_paise"]
    ledger += _cards(
        [
            (label, _money(totals[key]))
            for label, key in [
                ("Reserved across scenarios", "liability_reserved"),
                ("Released", "liability_released"),
                ("Qualified / committed", "payment_qualified_benefit_owed"),
                ("Benefit fulfilled", "benefit_fulfilled"),
            ]
        ]
    )
    owed = sum(row["amount_paise"] for row in replay["owed_not_fulfilled"])
    ledger += f"<p><strong>Still owed: {_money(owed)}.</strong> Final Bank B commitment covers two purchases: {_money(check['fulfilled_total_paise'])} fulfilled and {_money(owed)} owed. An empty reconciliation queue does not mean all cashback has arrived.</p>"
    ledger += f"<p>Event-derived funding matches native balances for every campaign: <strong>{_safe(check['replay_matches_native'])}</strong>. All {_safe(check['table_count'])} exported tables and {_safe(check['report_count'])} reports passed their canonical JSON hash checks.</p>"
    boundary = _table(
        ["Component", "Observed status"],
        [
            ("Python / SQLite / fake provider", "Run end to end; no real money sent"),
            (
                "Source → serving bundle → application → export",
                "Executed locally; retained JSON verified",
            ),
            (
                "Databricks preparation / Delta import / SQL",
                "Notebook Python syntax checked; SQL supplied; workspace not executed",
            ),
            (
                "Current API / worker / PostgreSQL adapter",
                "Implemented; separate local verification is described in the runtime guide",
            ),
            (
                "Lakebase / managed sync / production identity / real partners",
                "Cloud execution and deployment integration remain unverified",
            ),
        ],
    )
    boundary += (
        f'<p class="digest">Source tree SHA-256: {_safe(manifest["source"]["tree_sha256"])}</p>'
    )
    boundary += "<p>Read-only verification reconstructs the report from exported events and compares it with native balances. File hashes check the retained evidence; they do not authenticate an untrusted publisher.</p>"
    return (
        ResultSection(
            "01-run-and-tests",
            "1. The complete run and tests",
            tests,
            ("run.log", "run_manifest.json"),
        ),
        ResultSection(
            "02-publication",
            "2. Approve complete offer rules",
            pub,
            ("serving_bundle.json", "exports/snapshots.json"),
        ),
        ResultSection(
            "03-offer-ranking",
            "3. Compare usable savings",
            ranks,
            ("batch/slate.json", "batch/decisions.json", "scenario_summary.json"),
        ),
        ResultSection(
            "04-benefit-lifecycle",
            "4. Reserve, earn, and receive the benefit",
            lifecycle,
            ("scenario_summary.json", "exports/events.json", "exports/benefit_obligations.json"),
        ),
        ResultSection(
            "05-failure-and-recovery",
            "5. Release safely and recover uncertainty",
            failures,
            (
                "scenario_summary.json",
                "exports/applications.json",
                "exports/inbox.json",
                "exports/outbox.json",
            ),
        ),
        ResultSection(
            "06-ledger-and-replay",
            "6. Reconcile every rupee",
            ledger,
            ("exports/budgets.json", "exports/replay_report.json"),
        ),
        ResultSection(
            "07-execution-boundary",
            "7. What this run establishes",
            boundary,
            ("evidence_manifest.json", "syntax_check.json", "exports/export_manifest.json"),
        ),
    )


STYLE = """:root{color-scheme:light;--ink:#24343d;--teal:#216c61;--indigo:#4b4c92;--paper:#f7f5ee}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:18px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif}main{max-width:1390px;margin:auto;padding:38px 44px 48px}.kicker{color:var(--teal);font-weight:750;letter-spacing:.14em;font-size:14px;text-transform:uppercase}h1{font-size:36px;line-height:1.17;margin:12px 0 16px;letter-spacing:-.8px}h2{font-size:29px;line-height:1.2;margin:0 0 24px}p{margin:16px 0}.meta{font-size:15px;color:#54646a;overflow-wrap:anywhere}.scope{padding:12px 18px;border-left:4px solid var(--teal);background:#e6f0e9;margin:22px 0 30px}section{padding:30px;background:#fff;border:1px solid #d9dfd9;border-radius:14px;margin:28px 0;box-shadow:0 4px 18px #24343d05}.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin:18px 0 24px}.card{padding:16px 18px;background:#eeeff8;border-top:3px solid #6e6eb0;border-radius:8px}.card span{display:block;color:#566270;font-size:14px}.card strong{display:block;font-size:27px;line-height:1.3;margin-top:6px;color:#34366d}.table-wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:16px}th,td{text-align:left;padding:13px 12px;border-bottom:1px solid #dde3df;vertical-align:top}th{font-size:13px;color:#4f5e67;text-transform:uppercase;letter-spacing:.04em;background:#f1f4f0}tbody tr:nth-child(even){background:#fafbf8}td:first-child{overflow-wrap:anywhere}code,pre,.digest{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}code{font-size:.86em;overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#172b34;color:#e5f5ee;padding:24px;border-radius:10px;font-size:15px;line-height:1.6}.digest{font-size:13px;overflow-wrap:anywhere;color:#566270}.sources{margin-top:22px;border-top:1px dashed #d8dfd9;padding-top:12px;font-size:13px;color:#647271}.shot{display:block;max-width:100%;height:auto;border:1px solid #d9dfd9;border-radius:8px}a{color:var(--teal);text-underline-offset:3px;overflow-wrap:anywhere}nav{font-size:15px;display:flex;gap:16px;flex-wrap:wrap}.snapshot-page main{max-width:none;padding:30px 40px}.snapshot-page h1{font-size:30px;margin-bottom:8px}.snapshot-page section{margin:22px 0 12px;padding:26px}.snapshot-page .scope{margin:18px 0}.snapshot-page .meta{font-size:13px}.snapshot-page td,.snapshot-page th{padding:12px}.snapshot-page .cards{margin-top:12px}.snapshot-page footer{font-size:13px;color:#5e6f72;margin-top:14px}@media(max-width:750px){main{padding:24px 16px}h1{font-size:30px}h2{font-size:24px}section{padding:18px}.cards{grid-template-columns:repeat(2,minmax(0,1fr))}table{font-size:13px}th,td{padding:9px}pre{font-size:12px}}@media print{body{background:white;font-size:12pt}main{padding:0;max-width:none}section{break-inside:avoid;box-shadow:none}nav,.shot{display:none}}
"""


def _page(title: str, body: str, snapshot: bool = False) -> str:
    css_class = ' class="snapshot-page"' if snapshot else ""
    return f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{_safe(title)}</title><style>{STYLE}</style></head><body{css_class}><main>{body}</main></body></html>\n'


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshots_current(output_dir: Path, evidence_hash: str, run_id: str) -> bool:
    path = output_dir / "snapshots/snapshot_manifest.json"
    if not path.is_file():
        return False
    try:
        manifest = json.loads(path.read_text())
        return (
            manifest["run_id"] == run_id
            and manifest["evidence_manifest_sha256"] == evidence_hash
            and all(
                _sha(output_dir / row["png"]) == row["png_sha256"] for row in manifest["snapshots"]
            )
            and all(
                _sha(output_dir / row["html"]) == row["html_sha256"]
                for row in manifest["snapshots"]
            )
            and len(manifest["snapshots"]) == 7
        )
    except (OSError, KeyError, ValueError):
        return False


def render_results(evidence_dir: Path, output_dir: Path) -> None:
    """Build docs from one validated evidence set without changing that evidence."""
    guide_root = Path(__file__).resolve().parents[1] / "docs"
    architecture_link, runtime_link, databricks_link = (
        quote(
            os.path.relpath(guide_root / name, output_dir.resolve()).replace(os.sep, "/"), safe="/"
        )
        for name in ("architecture.md", "runtime.md", "databricks.md")
    )
    data = load_report_data(evidence_dir)
    manifest, summary, replay = data.manifest, data.summary, data.replay
    slate, metrics = data.slate, data.metrics
    bank_b = next(row for row in slate if row["offer_id"] == "diwali_b_cashback")
    bank_a = next(row for row in slate if row["offer_id"] == "diwali_a_instant")
    bank_b_budget = next(
        row for row in replay["conservation"] if row["offer_id"] == "diwali_b_cashback"
    )
    check = manifest["verification"]
    totals = replay["replay_totals_paise"]
    sections = pure_sections(data)
    views = output_dir / "views"
    views.mkdir(parents=True, exist_ok=True)
    run_id = manifest["run_id"]
    header = '<div class="kicker">Observed local results · synthetic data</div><h1>Festive offer ranking: the complete result</h1>'
    header += f'<div class="meta">Run <code>{_safe(run_id)}</code> · Captured {_safe(manifest["captured_at_utc"])} · Python {_safe(manifest["runtime"]["python"])}</div>'
    scope = '<div class="scope">Screenshots of a report generated from saved SQLite and fake-provider evidence. The current API, worker and PostgreSQL adapter have separate local verification. Cloud workspace/Lakebase execution, production identity deployment, managed sync and real partners remain unverified.</div>'
    content = header + scope
    content += (
        "<nav>"
        + "".join(f'<a href="#{part.slug}">{_safe(part.title)}</a>' for part in sections)
        + "</nav>"
    )
    for part in sections:
        sources = (
            '<div class="sources">Evidence: '
            + " · ".join(f"<code>{_safe(p)}</code>" for p in part.sources)
            + "</div>"
        )
        section = (
            f'<section id="{part.slug}"><h2>{_safe(part.title)}</h2>{part.body}{sources}</section>'
        )
        content += section
        view = (
            header
            + scope
            + section
            + f"<footer>Saved stage evidence for {_safe(run_id)} · source {_safe(manifest['source']['tree_sha256'][:20])}…</footer>"
        )
        (views / f"{part.slug}.html").write_text(_page(part.title, view, snapshot=True))
    content += f'<section><h2>Raw records and reproduction</h2><p><a href="readme.md">Results guide</a> · <a href="evidence/evidence_manifest.json">Evidence manifest</a> · <a href="evidence/run.log">Full run transcript</a> · <a href="{_safe(databricks_link)}">Databricks execution guide</a></p><p>See the results guide for read-only verification and screenshot regeneration commands.</p></section>'
    (output_dir / "index.html").write_text(
        _page("Festive offer ranking — observed results", content)
    )
    evidence_hash = data.evidence_manifest_sha256
    rendered_paths = ["index.html"] + [f"views/{part.slug}.html" for part in sections]
    rendered_manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "evidence_manifest_sha256": evidence_hash,
        "source_tree_sha256": manifest["source"]["tree_sha256"],
        "files": {relative: _sha(output_dir / relative) for relative in rendered_paths},
    }
    (output_dir / "render_manifest.json").write_text(json.dumps(rendered_manifest, indent=2) + "\n")
    snapshots_current = _snapshots_current(output_dir, evidence_hash, run_id)
    fence = chr(96) * 3
    lines = [
        "# Observed end-to-end results",
        "",
        "This record comes from one executed synthetic local run. It covers source validation, publication, ranking, reservation, simulated payment, separate cashback confirmation, failure/recovery, and analytical export. Screenshots show a report of retained data, rather than a Databricks or live-payment interface.",
        "",
        f"**Run:** `{run_id}`. **Captured:** {manifest['captured_at_utc']}. **Runtime:** Python {manifest['runtime']['python']}. **Application tests:** {format_test_result(manifest['test_result'])}; process exit {manifest['exit_code']}.",
        "",
        f"[Open the complete HTML report](index.html) · [Full test/run transcript](evidence/run.log) · [Evidence manifest](evidence/evidence_manifest.json) · [Source code and SOLID boundaries]({architecture_link})",
        "",
        "## Reproduce and verify",
        "",
        "From the repository root:",
        "",
        fence + "bash",
        "./record_results.sh --run-id my_results_01 --destination docs/my-results/evidence --snapshots",
        "./record_results.sh --verify-run output/my_results_01",
        "./record_results.sh --verify-run docs/my-results/evidence",
        fence,
        "",
        "Use a new run ID and destination for each capture. The recorder refuses an existing run; `--replace` replaces only a results evidence destination. Screenshot generation needs a local Chromium-based browser; the application and evidence verifier use the Python standard library. The recorder checks source identity before and after execution.",
        "",
        f"To verify the saved application run used here: `{f'./record_results.sh --verify-run output/{run_id}'}`. This reads exported JSON, checks the completion and content hashes, and reconstructs ledger totals from events without sending a payment or modifying the database.",
        "",
        "The checked-in evidence is portable. Its manifest hashes bind the copied files to this capture. If the ignored original output directory is absent, retain the evidence folder and its manifest together; the read-only verification command also accepts that evidence folder. To regenerate only screenshots from retained views, use `python3 -m tools.capture_snapshots --results docs/my-results --replace`.",
        "",
        "## Reading the result",
        "",
        f"- Publication approves {check['approved_count']} of {check['source_count']} source offers. The missing-cap row is quarantined.",
        f"- Bank B ranks first at {_money(bank_b['expected_benefit_paise'])} expected savings from {_money(bank_b['nominal_benefit_paise'])} nominal cashback. Applying it reserves the full {_money(bank_b['nominal_benefit_paise'])}. Bank A saves {_money(bank_a['nominal_benefit_paise'])} immediately.",
        "- The primary application moves from HELD to cashback OWED, then to FULFILLED only after separate evidence.",
        f"- A {_money(summary['definitive_failure']['nominal_paise'])} failed-payment hold is released. A separate {_money(summary['recovered_application']['nominal_paise'])} uncertain attempt stays held until original-reference reconciliation establishes capture; its cashback remains OWED.",
        f"- Final Bank B funding is {_money(bank_b_budget['funding_paise'])} = {_money(bank_b_budget['available_paise'])} available + {_money(bank_b_budget['held_paise'])} held + {_money(bank_b_budget['committed_paise'])} committed. Committed funding covers the fulfilled purchase and the still-owed purchase.",
        f"- Replay totals across the scenarios are {_money(totals['liability_reserved'])} reserved, {_money(totals['liability_released'])} released, {_money(totals['payment_qualified_benefit_owed'])} qualified, and {_money(totals['benefit_fulfilled'])} fulfilled. Fulfillment does not make that spent funding available again.",
        "",
        f"The {check['batch_decision_count']} raw batch decisions and {check['batch_ranked_count']} ranked rows include the incomplete contract refusal. The locally verified approved-only path has {check['approved_decision_count']} decisions across {metrics['request_count']} fixture checkouts and {check['approved_ranked_count']} ranked rows. Synthetic scoring probabilities and amounts are not production measurements.",
        "",
        "## Snapshots and raw evidence",
        "",
    ]
    for part in sections:
        lines.extend([f"### {part.title}", ""])
        if snapshots_current:
            lines.extend(
                [f"![{part.title}: captured local result report](snapshots/{part.slug}.png)", ""]
            )
        else:
            lines.extend(
                [
                    "PNG snapshot has not been captured for this evidence set. Open the retained view below or run the screenshot command above.",
                    "",
                ]
            )
        lines.extend(
            [
                f"[Standalone rendered view](views/{part.slug}.html). Raw evidence: "
                + ", ".join(f"[{p}](evidence/{p})" for p in part.sources)
                + ".",
                "",
            ]
        )
    lines.extend(
        [
            "## Execution boundaries",
            "",
            f"This capture used SQLite and a fake provider. The current API, worker and separately tested PostgreSQL adapter are described in [the runtime guide]({runtime_link}). Databricks notebook Python passed syntax checks; SQL is supplied. Cloud workspace/Lakebase execution, production identity deployment, managed sync and real partners remain unverified. [Follow the cloud handoff steps]({databricks_link}).",
            "",
            "The [render manifest](render_manifest.json) binds each view to this evidence set. A snapshot manifest records the run ID, each standalone HTML hash, PNG hash, browser version, viewport, and capture command. "
            + (
                "[Snapshot manifest](snapshots/snapshot_manifest.json). "
                if snapshots_current
                else "No current snapshot manifest exists yet. "
            )
            + "A screenshot is a reading aid; the JSON, transcript, source digest, and read-only replay are the reproducible evidence.",
            "",
            "All inputs, wallets, payment events, and amounts are synthetic. The public fixture signing key is not a credential. Hashes establish internal file integrity and do not authenticate an untrusted publisher.",
            "",
        ]
    )
    (output_dir / "readme.md").write_text("\n".join(lines))


def main() -> None:
    """Refresh report views from verified retained evidence without running a payment."""
    from .record_results import verify_saved_run

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "docs/results/evidence",
    )
    args = parser.parse_args()
    evidence = args.evidence.resolve()
    if evidence.name != "evidence":
        parser.error("Rendered result evidence must use <results>/evidence")
    verify_saved_run(evidence)
    render_results(evidence, evidence.parent)
    print(f"Rendered verified evidence: {evidence.parent}")


if __name__ == "__main__":
    main()
