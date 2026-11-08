from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote

DEMO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(DEMO_ROOT))
sys.path.insert(0, str(DEMO_ROOT / "src"))

from festive_offers.online.models import Application
from festive_offers.online.reporting_service import ReportingService
from festive_offers.pipeline import run_batch
from festive_offers.serialization import dataset_from_dict
from tools.evidence_files import SOURCE_ALGORITHM, load_report_data
from tools.evidence_models import SourceTree
from tools.record_results import capture_boundaries, compile_sources, source_tree, verify_saved_run
from tools.results_evidence import (
    REPORTS,
    TABLES,
    ExportReportUnits,
    canonical_sha256,
    format_test_result,
    parse_test_result,
    validate_run,
)
from tools.results_render import pure_sections, render_results


def fixture_artifacts() -> dict:
    """Create a small saved-output fixture with one delivered and one owed benefit."""
    source = {
        "offers": [
            {
                "offer_id": "offer_1",
                "issuer": "Synthetic Bank",
                "merchant": "shop",
                "starts_at": "2026-11-01T00:00:00Z",
                "ends_at": "2026-11-15T00:00:00Z",
                "rate": "0.10",
                "min_eligible_spend_paise": 0,
                "cap_paise": 200,
                "instrument_rules": [
                    {
                        "product": "synthetic_card",
                        "network": "visa",
                        "payment_mode": "purchase",
                    }
                ],
                "included_categories": ["electronics"],
                "excluded_brands": [],
                "payout": "cashback",
                "realization_key": "delayed",
                "currency": "INR",
                "catalog_version": "fixture",
            }
        ],
        "assumptions": {"delayed": "0.80"},
        "wallets": [],
        "checkouts": [],
    }
    body = {"offers": source["offers"], "assumptions": source["assumptions"], "quarantined": []}
    manifest = {
        "revision": "revision_1",
        "schema_version": 1,
        "scoring_version": "expected_savings_v1",
        "source_count": 1,
        "approved_count": 1,
        "quarantine_count": 0,
        "content_sha256": canonical_sha256(body),
    }
    bundle = {"manifest": manifest, **body}
    artifacts = {f"exports/{name}.json": [] for name in TABLES}
    artifacts["exports/active_snapshot.json"] = [{"revision": "revision_1"}]
    artifacts["exports/snapshots.json"] = [
        {"revision": "revision_1", "bundle_json": json.dumps(bundle)}
    ]
    artifacts["exports/budgets.json"] = [
        {
            "offer_id": "offer_1",
            "funding_paise": 500,
            "available_paise": 200,
            "held_paise": 0,
            "committed_paise": 300,
        }
    ]
    artifacts["exports/applications.json"] = [
        {
            "application_id": "app_1",
            "payment_id": "payment_1",
            "provider_reference": "provider_1",
            "quote_id": "quote_1",
            "offer_id": "offer_1",
            "nominal_paise": 100,
            "state": "FULFILLED",
            "payment_state": "CAPTURED",
            "benefit_state": "FULFILLED",
            "budget_state": "COMMITTED",
        },
        {
            "application_id": "app_2",
            "payment_id": "payment_2",
            "provider_reference": "provider_2",
            "quote_id": "quote_2",
            "offer_id": "offer_1",
            "nominal_paise": 200,
            "state": "OWED",
            "payment_state": "CAPTURED",
            "benefit_state": "OWED",
            "budget_state": "COMMITTED",
        },
        {
            "application_id": "app_3",
            "payment_id": "payment_3",
            "provider_reference": "provider_3",
            "quote_id": "quote_3",
            "offer_id": "offer_1",
            "nominal_paise": 50,
            "state": "RELEASED",
            "payment_state": "FAILED",
            "benefit_state": "NONE",
            "budget_state": "RELEASED",
        },
    ]
    artifacts["exports/quotes.json"] = [
        {
            "quote_id": "quote_1",
            "decisions_json": json.dumps([{"offer_id": "offer_1", "expected_benefit_paise": 80}]),
        }
    ]
    artifacts["exports/benefit_obligations.json"] = [
        {"application_id": "app_1", "state": "FULFILLED", "amount_paise": 100},
        {"application_id": "app_2", "state": "OWED", "amount_paise": 200},
    ]
    events = []

    def event(app_id, kind, **payload):
        events.append(
            {
                "event_sequence": len(events) + 1,
                "application_id": app_id,
                "kind": kind,
                "payload_json": json.dumps(payload),
            }
        )

    event("app_1", "liability_reserved", amount_paise=100)
    event("app_1", "dispatch_claimed", provider_reference="provider_1")
    event("app_1", "payment_qualified_benefit_owed", amount_paise=100)
    event("app_1", "benefit_fulfilled", amount_paise=100)
    event("app_3", "liability_reserved", amount_paise=50)
    event("app_3", "liability_released", amount_paise=50, reason="provider_definitive_failure")
    event("app_2", "liability_reserved", amount_paise=200)
    event("app_2", "dispatch_result", state="UNKNOWN", reason="TimeoutError")
    event("app_2", "payment_qualified_benefit_owed", amount_paise=200)
    artifacts["exports/events.json"] = events
    report = ReportingService(ExportReportUnits(artifacts)).report()
    artifacts["exports/replay_report.json"] = report
    artifacts["exports/reconciliation_report.json"] = report["reconciliation_required"]

    def view(row, **overrides):
        return {**{key: row[key] for key in Application.__dataclass_fields__}, **overrides}

    primary, recovered, failed = artifacts["exports/applications.json"]
    raw_batch = run_batch(dataset_from_dict(source), "fixture")
    artifacts.update(
        {
            "run_manifest.json": {
                "run_id": "fixture",
                "completed": True,
                "source_canonical_sha256": canonical_sha256(source),
                "bundle_content_sha256": manifest["content_sha256"],
            },
            "scenario_summary.json": {
                "run_id": "fixture",
                "publication_manifest": manifest,
                "funding_conserved": True,
                "batch_decisions": 0,
                "batch_ranked": 0,
                "quoted_offer_expected_paise": {"offer_1": 80},
                "duplicate_payment_refused": True,
                "primary_application_stages": {
                    "reserved": view(
                        primary,
                        state="HELD",
                        payment_state="NOT_SENT",
                        benefit_state="NONE",
                        budget_state="HELD",
                    ),
                    "payment_qualified_cashback_owed": view(
                        primary, state="OWED", benefit_state="OWED"
                    ),
                    "cashback_confirmed": view(primary),
                },
                "definitive_failure": view(failed),
                "uncertain_after_dispatch": view(
                    recovered,
                    state="UNKNOWN",
                    payment_state="UNKNOWN",
                    benefit_state="NONE",
                    budget_state="HELD",
                ),
                "recovered_application": view(recovered),
                "lookup_without_evidence": "no_definitive_provider_evidence",
                "lookup_after_capture": "applied",
            },
            "source_dataset.json": source,
            "serving_bundle.json": bundle,
            "batch/decisions.json": [],
            "batch/slate.json": [],
            "batch/metrics.json": raw_batch.metrics,
            "exports/export_manifest.json": {
                "schema_version": 1,
                "canonical_json_sha256": {
                    f"{name}.json": canonical_sha256(artifacts[f"exports/{name}.json"])
                    for name in TABLES + REPORTS
                },
            },
        }
    )
    return artifacts


def captured_fixture(directory: Path) -> dict:
    """Write the portable evidence envelope around the pure saved-output fixture."""
    artifacts = fixture_artifacts()
    for relative, value in artifacts.items():
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value) + "\n", encoding="utf-8")
    (directory / "run.log").write_text("Ran 7 tests in 0.123s\n\nOK\n", encoding="utf-8")
    files = {"src/historical.py": "a" * 64}
    syntax = {
        "schema_version": 1,
        "status": "OK",
        "python": "3.12.6",
        "implementation": "CPython",
        "source_tree_sha256": canonical_sha256(files),
        "checked_count": 1,
        "checked_files": [{"path": "src/historical.py", "sha256": "a" * 64, "status": "OK"}],
    }
    (directory / "syntax_check.json").write_text(json.dumps(syntax), encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "run_id": "fixture",
        "exit_code": 0,
        "runtime": {"python": "3.12.6", "implementation": "CPython"},
        "test_result": {"count": 7, "duration_seconds": 0.123, "status": "OK"},
        "verification": validate_run(artifacts, "fixture"),
        "source": {"files": files, "tree_sha256": canonical_sha256(files)},
        "artifacts": {
            path.relative_to(directory).as_posix(): {
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size_bytes": path.stat().st_size,
            }
            for path in directory.rglob("*")
            if path.is_file()
        },
    }
    (directory / "evidence_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


class EvidenceValidationTests(unittest.TestCase):
    def test_success_uses_actual_log_count_and_keeps_owed_benefits_visible(self) -> None:
        tests = parse_test_result(
            "captured command\nRan 7 tests in 0.123s\n\nOK\nscenario completed\n"
        )
        self.assertEqual(tests, {"count": 7, "duration_seconds": 0.123, "status": "OK"})
        self.assertEqual(format_test_result(tests), "7 passed")
        result = validate_run(fixture_artifacts(), "fixture")
        self.assertEqual((result["table_count"], result["canonical_hashes_verified"]), (13, 15))
        self.assertEqual(
            (
                result["owed_count"],
                result["owed_total_paise"],
                result["reconciliation_required_count"],
            ),
            (1, 200, 0),
        )

    def test_skipped_tests_are_discovered_but_never_reported_as_passed(self) -> None:
        tests = parse_test_result("Ran 91 tests in 0.123s\n\nOK (skipped=14)\n")
        self.assertEqual(
            tests,
            {
                "count": 77,
                "duration_seconds": 0.123,
                "status": "OK",
                "discovered": 91,
                "skipped": 14,
            },
        )
        self.assertEqual(format_test_result(tests), "77 passed, 14 skipped (91 discovered)")
        all_skipped = parse_test_result("Ran 2 tests in 0.000s\n\nOK (skipped=2)\n")
        self.assertEqual(format_test_result(all_skipped), "0 passed, 2 skipped (2 discovered)")

    def test_unsupported_or_inconsistent_success_summaries_are_refused(self) -> None:
        suffixes = (
            "(expected failures=1)",
            "(skipped=2, expected failures=1)",
            "(skipped=8)",
            "(skipped=-1)",
            "trailing-unrecognized-text",
        )
        for suffix in suffixes:
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                parse_test_result(f"Ran 7 tests in 0.123s\n\nOK {suffix}\n")
        with self.assertRaises(ValueError):
            parse_test_result(
                "Ran 7 tests in 0.123s\n\nOK\nRan 8 tests in 0.456s\n\nOK (skipped=1)\n"
            )

    def test_retained_historical_evidence_keeps_its_original_test_envelope(self) -> None:
        root = DEMO_ROOT / "docs/results/evidence"
        original = json.loads((root / "evidence_manifest.json").read_text())
        verified = verify_saved_run(root)
        self.assertEqual(set(verified["test_result"]), {"count", "duration_seconds", "status"})
        self.assertEqual(verified["test_result"], original["test_result"])

    def test_skipped_evidence_replays_and_renders_accurate_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = root / "evidence"
            shutil.copytree(DEMO_ROOT / "docs/results/evidence", evidence)
            manifest = json.loads((evidence / "evidence_manifest.json").read_text())
            log = evidence / "run.log"
            log.write_text("Ran 91 tests in 0.123s\n\nOK (skipped=14)\n", encoding="utf-8")
            manifest["test_result"] = parse_test_result(log.read_text())
            manifest["artifacts"]["run.log"] = {
                "sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
                "size_bytes": log.stat().st_size,
            }
            (evidence / "evidence_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(verify_saved_run(evidence)["test_result"]["count"], 77)
            render_results(evidence, root)
            for relative in ("readme.md", "index.html", "views/01-run-and-tests.html"):
                rendered = (root / relative).read_text()
                self.assertIn("77 passed, 14 skipped (91 discovered)", rendered)
                self.assertNotIn("91 passed", rendered)
                self.assertNotIn("Deployment adapters and integration tests remain", rendered)
                self.assertNotIn("service deployment/authentication", rendered)
            guide = (root / "readme.md").read_text()
            for label, filename in (
                ("the runtime guide", "runtime.md"),
                ("Source code and SOLID boundaries", "architecture.md"),
                ("Follow the cloud handoff steps", "databricks.md"),
            ):
                match = re.search(r"\[" + re.escape(label) + r"\]\(([^)]+)\)", guide)
                self.assertIsNotNone(match)
                target = (root / unquote(match.group(1))).resolve()
                self.assertEqual(target, (DEMO_ROOT / "docs" / filename).resolve())
                self.assertTrue(target.is_file())

    def test_incomplete_or_failed_execution_cannot_be_reported_as_passed(self) -> None:
        for log in (
            "Ran 7 tests in 0.1s\n\nFAILED (failures=1)\n",
            "scenario completed without a test summary",
        ):
            with self.subTest(log=log), self.assertRaises(ValueError):
                parse_test_result(log)
        artifacts = fixture_artifacts()
        artifacts["run_manifest.json"]["completed"] = False
        with self.assertRaisesRegex(ValueError, "completion"):
            validate_run(artifacts, "fixture")
        artifacts = fixture_artifacts()
        artifacts["exports/snapshots.json"][0]["bundle_json"] = "{}"
        artifacts["exports/export_manifest.json"]["canonical_json_sha256"]["snapshots.json"] = (
            canonical_sha256(artifacts["exports/snapshots.json"])
        )
        with self.assertRaisesRegex(ValueError, "native snapshot"):
            validate_run(artifacts, "fixture")

    def test_corrupted_export_and_rehashed_replay_mismatch_are_refused(self) -> None:
        artifacts = fixture_artifacts()
        artifacts["exports/budgets.json"][0]["available_paise"] = 199
        with self.assertRaisesRegex(ValueError, "Export hash mismatch"):
            validate_run(artifacts, "fixture")
        artifacts = copy.deepcopy(fixture_artifacts())
        artifacts["exports/events.json"][0]["payload_json"] = json.dumps({"amount_paise": 101})
        artifacts["exports/export_manifest.json"]["canonical_json_sha256"]["events.json"] = (
            canonical_sha256(artifacts["exports/events.json"])
        )
        with self.assertRaisesRegex(ValueError, "replay/reconciliation"):
            validate_run(artifacts, "fixture")

    def test_portable_evidence_verifies_actual_log_and_rejects_changed_test_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            manifest = captured_fixture(directory)
            verified = verify_saved_run(directory)
            self.assertEqual(verified["test_result"]["count"], 7)
            self.assertEqual(
                verified["captured_source_tree_sha256"], manifest["source"]["tree_sha256"]
            )
            log = directory / "run.log"
            log.write_text("Ran 8 tests in 0.123s\n\nOK\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "byte hash"):
                verify_saved_run(directory)
            manifest["artifacts"]["run.log"] = {
                "sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
                "size_bytes": log.stat().st_size,
            }
            (directory / "evidence_manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "actual captured test log"):
                verify_saved_run(directory)

    def test_portable_evidence_rejects_changed_batch_bytes_and_unsafe_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            manifest = captured_fixture(directory)
            # Semantically identical JSON must still match its captured bytes.
            (directory / "batch/slate.json").write_text("[\n]\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "byte hash"):
                verify_saved_run(directory)
            manifest["artifacts"]["../outside.json"] = {"sha256": "a" * 64, "size_bytes": 1}
            (directory / "evidence_manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                ValueError, "complete captured artifact set|Unsafe evidence"
            ):
                verify_saved_run(directory)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = root / "evidence"
            evidence.mkdir()
            captured_fixture(evidence)
            outside = root / "outside.json"
            outside.write_text("{}", encoding="utf-8")
            (evidence / "linked.json").symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "escapes its folder"):
                verify_saved_run(evidence)

    def test_displayed_scenario_states_money_and_metrics_must_match_actual_records(self) -> None:
        mutations = (
            (
                "scenario_summary.json",
                ("primary_application_stages", "reserved", "state"),
                "FULFILLED",
            ),
            ("scenario_summary.json", ("definitive_failure", "state"), "FULFILLED"),
            ("scenario_summary.json", ("uncertain_after_dispatch", "budget_state"), "RELEASED"),
            ("scenario_summary.json", ("recovered_application", "nominal_paise"), 1),
            ("scenario_summary.json", ("quoted_offer_expected_paise", "offer_1"), 999),
            ("batch/metrics.json", ("request_count",), 999),
        )
        for artifact, keys, value in mutations:
            with self.subTest(artifact=artifact, keys=keys):
                artifacts = fixture_artifacts()
                target = artifacts[artifact]
                for key in keys[:-1]:
                    target = target[key]
                target[keys[-1]] = value
                with self.assertRaises(ValueError):
                    validate_run(artifacts, "fixture")
        artifacts = fixture_artifacts()
        artifacts["exports/events.json"][1]["event_sequence"] = 100
        artifacts["exports/export_manifest.json"]["canonical_json_sha256"]["events.json"] = (
            canonical_sha256(artifacts["exports/events.json"])
        )
        with self.assertRaisesRegex(ValueError, "journal is out of order"):
            validate_run(artifacts, "fixture")

    def test_syntax_check_is_byte_bound_does_not_execute_and_rejects_broken_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            (root / "data").mkdir()
            source = root / "src" / "optional.py"
            source.write_text(
                "import deliberately_unavailable_library\nraise RuntimeError('must not execute')\n",
                encoding="utf-8",
            )
            fixture = root / "data" / "dataset.json"
            fixture.write_text("{}", encoding="utf-8")
            before = source_tree(root)
            self.assertIn("data/dataset.json", before["files"])
            checked = compile_sources(root, before)
            self.assertEqual((checked["status"], checked["checked_count"]), ("OK", 1))
            self.assertFalse((root / "src" / "__pycache__").exists())
            fixture.write_text('{"changed":true}', encoding="utf-8")
            self.assertNotEqual(before["tree_sha256"], source_tree(root)["tree_sha256"])
            source.write_text("def broken(:\n", encoding="utf-8")
            with self.assertRaises(SyntaxError):
                compile_sources(root, source_tree(root))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = captured_fixture(root)
            syntax_file = root / "syntax_check.json"
            syntax = json.loads(syntax_file.read_text())
            syntax["checked_files"][0]["sha256"] = "b" * 64
            syntax_file.write_text(json.dumps(syntax), encoding="utf-8")
            manifest["artifacts"]["syntax_check.json"] = {
                "sha256": hashlib.sha256(syntax_file.read_bytes()).hexdigest(),
                "size_bytes": syntax_file.stat().st_size,
            }
            (root / "evidence_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "recorded syntax check"):
                verify_saved_run(root)

    def test_browser_deployment_and_dependency_inputs_are_bound_but_private_outputs_are_not(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = {
                "web/index.html": "<main>Reference checkout</main>\n",
                "web/app.js": "const state = 'HELD';\n",
                "web/style.css": "main { color: navy; }\n",
                "tools/browser_qa.cjs": "console.log('browser assertions');\n",
                "deployment/jobs.yml": "resources: {}\n",
                "databricks.yml": "bundle: {name: reference}\n",
                "requirements.txt": "# standard library\n",
                "requirements-postgres.txt": "psycopg>=3.2,<4\n",
            }
            for relative, content in sources.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            original = source_tree(root)
            self.assertTrue(set(sources) <= original["files"].keys())
            for relative, content in sources.items():
                with self.subTest(source=relative):
                    path = root / relative
                    path.write_text(content + "\n", encoding="utf-8")
                    self.assertNotEqual(source_tree(root)["tree_sha256"], original["tree_sha256"])
                    path.write_text(content, encoding="utf-8")
            for relative in (
                "output/report.js",
                ".venv/helper.py",
                "runtime.env",
                "web/runtime.env",
                "tools/__pycache__/generated.py",
                "web/output/generated.js",
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("private or generated test fixture\n", encoding="utf-8")
            self.assertEqual(source_tree(root), original)

    def test_changed_root_python_configuration_changes_source_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configuration = root / "pyproject.toml"
            configuration.write_text("[tool.ruff]\nline-length = 100\n", encoding="utf-8")
            before = source_tree(root)
            self.assertIn("pyproject.toml", before["files"])
            configuration.write_text("[tool.ruff]\nline-length = 110\n", encoding="utf-8")
            after = source_tree(root)
            self.assertNotEqual(after["tree_sha256"], before["tree_sha256"])
            self.assertNotEqual(after["files"]["pyproject.toml"], before["files"]["pyproject.toml"])

    def test_source_and_compile_paths_cannot_escape_the_captured_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = parent / "captured"
            (root / "src").mkdir(parents=True)
            private_bytes = b"raise RuntimeError('outside fixture must not be read')\n"
            outside = parent / "outside.py"
            outside.write_bytes(private_bytes)
            linked = root / "src" / "linked.py"
            linked.symlink_to(outside)
            original_read = Path.read_bytes

            def guarded_read(path: Path) -> bytes:
                self.assertNotEqual(
                    path.resolve(), outside.resolve(), "An outside source file was read"
                )
                return original_read(path)

            with patch.object(Path, "read_bytes", guarded_read):
                with self.assertRaisesRegex(ValueError, "Source path escapes its folder"):
                    source_tree(root)
                for relative in ("../outside.py", outside.as_posix(), "src/linked.py"):
                    files = {relative: hashlib.sha256(private_bytes).hexdigest()}
                    source: SourceTree = {
                        "tree_sha256": canonical_sha256(files),
                        "files": files,
                        "algorithm": SOURCE_ALGORITHM,
                    }
                    with (
                        self.subTest(path=relative),
                        self.assertRaisesRegex(
                            ValueError, "Unsafe Source path|Source path escapes its folder"
                        ),
                    ):
                        compile_sources(root, source)

    def test_legacy_capture_imports_keep_the_files_adapter_identity(self) -> None:
        from tools import evidence_files, record_results

        self.assertIs(record_results.source_tree, evidence_files.source_tree)
        self.assertIs(record_results.compile_sources, evidence_files.compile_sources)
        self.assertIs(record_results.load_artifacts, evidence_files.load_artifacts)

    def test_new_capture_boundaries_separate_requested_postgres_tests_from_cloud_execution(
        self,
    ) -> None:
        default = capture_boundaries({})
        self.assertEqual(default["scenario_store"], "SQLite")
        self.assertFalse(default["postgresql_tests_requested"])
        self.assertFalse(default["lakebase_cloud_executed"])
        self.assertFalse(default["databricks_workspace_executed"])
        self.assertFalse(default["real_provider_executed"])
        self.assertNotIn("postgres_lakebase_executed", default)
        self.assertEqual(
            default["postgresql_test_evidence"], "Per-test statuses are recorded in run.log"
        )
        private_dsn = "postgresql://synthetic-private-coordinate/not-recorded"
        for environment in (
            {"FESTIVE_TEST_POSTGRES_DSN": private_dsn},
            {"FESTIVE_TEST_EMBEDDED_POSTGRES": "1"},
        ):
            boundary = capture_boundaries(environment)
            self.assertTrue(boundary["postgresql_tests_requested"])
            self.assertFalse(boundary["lakebase_cloud_executed"])
            self.assertNotIn(private_dsn, json.dumps(boundary))
        self.assertFalse(
            capture_boundaries({"FESTIVE_TEST_EMBEDDED_POSTGRES": "0"})[
                "postgresql_tests_requested"
            ]
        )

    def test_render_reads_each_evidence_input_once(self) -> None:
        evidence = (DEMO_ROOT / "docs/results/evidence").resolve()
        expected = {
            "evidence_manifest.json",
            "scenario_summary.json",
            "exports/replay_report.json",
            "batch/slate.json",
            "batch/metrics.json",
            "serving_bundle.json",
            "run.log",
        }
        counts: dict[str, int] = {}
        original_read = Path.read_bytes

        def counted_read(path: Path) -> bytes:
            resolved = path.resolve()
            if resolved.is_relative_to(evidence):
                relative = resolved.relative_to(evidence).as_posix()
                counts[relative] = counts.get(relative, 0) + 1
            return original_read(path)

        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(Path, "read_bytes", counted_read):
                render_results(evidence, Path(temporary))
        self.assertEqual(counts, {relative: 1 for relative in expected})

    def test_pure_sections_use_the_loaded_context_without_filesystem_reads(self) -> None:
        data = load_report_data(DEMO_ROOT / "docs/results/evidence")
        expected = pure_sections(data)
        with (
            patch.object(Path, "read_bytes", side_effect=AssertionError("Rendering reread a file")),
            patch.object(Path, "read_text", side_effect=AssertionError("Rendering reread a file")),
        ):
            self.assertEqual(pure_sections(data), expected)


if __name__ == "__main__":
    unittest.main()
