"""CLI/filesystem adapter for validated local result evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .evidence_files import MAX_JSON_BYTES as MAX_JSON_BYTES
from .evidence_files import compile_sources, load_artifacts, source_tree
from .evidence_models import CaptureBoundaries
from .results_evidence import canonical_sha256, format_test_result, parse_test_result, validate_run

DEMO_ROOT = Path(__file__).resolve().parents[1]


def capture_boundaries(environ: Mapping[str, str] | None = None) -> CaptureBoundaries:
    """Record requested test flags without exposing coordinates or claiming execution."""
    env = os.environ if environ is None else environ
    return {
        "execution": "local SQLite + fake provider",
        "scenario_store": "SQLite",
        "databricks_workspace_executed": False,
        "lakebase_cloud_executed": False,
        "real_provider_executed": False,
        "postgresql_tests_requested": bool(env.get("FESTIVE_TEST_POSTGRES_DSN"))
        or env.get("FESTIVE_TEST_EMBEDDED_POSTGRES") == "1",
        "postgresql_test_evidence": "Per-test statuses are recorded in run.log",
    }


def verify_saved_run(run_directory: Path) -> dict[str, Any]:
    """Verify saved output identity/accounting; this neither executes tests nor dispatches work."""
    artifacts = load_artifacts(run_directory)
    run_id = artifacts["run_manifest.json"]["run_id"]
    verification = validate_run(artifacts, run_id)
    result = {"run_id": run_id, "test_execution_repeated": False, **verification}
    captured = artifacts.get("evidence_manifest.json")
    if captured is not None:
        result.update(verify_evidence_manifest(run_directory, artifacts, captured, verification))
    return result


def verify_evidence_manifest(
    directory: Path,
    artifacts: dict[str, Any],
    manifest: dict[str, Any],
    verification: dict[str, Any],
) -> dict[str, Any]:
    """Check portable capture bytes and historical provenance without comparing current source."""
    if (
        manifest.get("schema_version") != 1
        or manifest.get("exit_code") != 0
        or manifest.get("run_id") != artifacts["run_manifest.json"]["run_id"]
    ):
        raise ValueError("The evidence capture identity or successful process exit is invalid")
    declared = manifest["artifacts"]
    expected = (set(artifacts) - {"evidence_manifest.json"}) | {"run.log"}
    if set(declared) != expected:
        raise ValueError(
            "The evidence manifest does not declare the complete captured artifact set"
        )
    root = directory.resolve()
    for relative, metadata in declared.items():
        path = PurePosixPath(relative)
        if path.is_absolute() or not path.parts or ".." in path.parts or "\\" in relative:
            raise ValueError(f"Unsafe evidence artifact path: {relative}")
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise ValueError(f"Evidence artifact escapes its folder or is absent: {relative}")
        content = target.read_bytes()
        if (
            len(content) != metadata["size_bytes"]
            or hashlib.sha256(content).hexdigest() != metadata["sha256"]
        ):
            raise ValueError(f"Evidence artifact byte hash or size mismatch: {relative}")
    test_result = parse_test_result((root / "run.log").read_text(encoding="utf-8"))
    if test_result != manifest["test_result"]:
        raise ValueError("The actual captured test log differs from its evidence manifest")
    if manifest["verification"] != verification:
        raise ValueError("The captured verification differs from regenerated saved-output checks")
    source = manifest["source"]
    if canonical_sha256(source["files"]) != source["tree_sha256"]:
        raise ValueError("The captured historical source digest is internally inconsistent")
    syntax = artifacts["syntax_check.json"]
    checked = {row["path"]: row for row in syntax["checked_files"]}
    python_files = {path: value for path, value in source["files"].items() if path.endswith(".py")}
    if (
        syntax.get("schema_version") != 1
        or syntax.get("status") != "OK"
        or syntax["source_tree_sha256"] != source["tree_sha256"]
        or syntax["python"] != manifest["runtime"]["python"]
        or syntax["implementation"] != manifest["runtime"]["implementation"]
        or syntax["checked_count"] != len(python_files)
        or len(checked) != len(syntax["checked_files"])
        or set(checked) != set(python_files)
        or any(
            row["status"] != "OK" or row["sha256"] != python_files[path]
            for path, row in checked.items()
        )
    ):
        raise ValueError(
            "The recorded syntax check does not match the captured Python source/runtime"
        )
    return {
        "evidence_file_hashes_verified": len(declared),
        "test_result": test_result,
        "captured_source_tree_sha256": source["tree_sha256"],
        "syntax_files_checked": syntax["checked_count"],
    }


def _check_destination(destination: Path, replace: bool) -> None:
    if destination.exists() and not destination.is_dir():
        raise ValueError("Evidence destination must be a directory")
    if destination.exists() and any(destination.iterdir()):
        if not replace:
            raise ValueError(
                "Evidence destination is nonempty; use --replace for a deliberate documentation refresh"
            )
        if not destination.is_relative_to(DEMO_ROOT / "docs"):
            raise ValueError(
                "--replace is limited to documentation evidence, never existing run output"
            )


def _publish_evidence(staging: Path, destination: Path, replace: bool) -> None:
    _check_destination(destination, replace)
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if destination.exists():
        backup = destination.with_name(f".{destination.name}-previous-{uuid.uuid4().hex}")
        destination.rename(backup)
    try:
        shutil.copytree(staging, destination)
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        if backup is not None:
            backup.rename(destination)
        raise
    if backup is not None:
        shutil.rmtree(backup)


def record(
    run_id: str, destination: Path, replace: bool, render: bool, snapshots: bool = False
) -> dict[str, Any]:
    """Capture an actual successful command and publish only evidence that verifies."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError("Run ID must contain only letters, digits, underscore or hyphen")
    if (render or snapshots) and destination.name != "evidence":
        raise ValueError(
            "Rendered results require a destination folder named 'evidence' so report links remain valid"
        )
    if snapshots and not render:
        raise ValueError("Snapshot capture requires result rendering")
    _check_destination(destination, replace)
    run_directory = DEMO_ROOT / "output" / run_id
    if run_directory.exists():
        raise ValueError(
            "The supplied run ID already exists; existing run output is never overwritten"
        )
    if destination == run_directory or destination.is_relative_to(run_directory):
        raise ValueError("Documentation evidence must be separate from the original run output")
    before = source_tree(DEMO_ROOT)
    boundaries = capture_boundaries()
    command = [str(DEMO_ROOT / "run_demo.sh"), "--run-id", run_id]
    started_at = datetime.now(timezone.utc).isoformat()
    start = time.perf_counter()
    with tempfile.TemporaryFile(mode="w+b") as capture:
        completed = subprocess.run(
            command, cwd=DEMO_ROOT, stdout=capture, stderr=subprocess.STDOUT, check=False
        )
        capture.seek(0)
        output = capture.read()
    captured_at = datetime.now(timezone.utc).isoformat()
    if completed.returncode != 0:
        failure_log = DEMO_ROOT / "output" / f"{run_id}.failed.log"
        failure_log.parent.mkdir(parents=True, exist_ok=True)
        failure_log.write_bytes(output)
        raise ValueError(
            f"The captured command failed with exit {completed.returncode}; log saved to {failure_log}"
        )
    try:
        syntax = compile_sources(DEMO_ROOT, before)
    except (ValueError, SyntaxError):
        failure_log = DEMO_ROOT / "output" / f"{run_id}.failed.log"
        failure_log.write_bytes(output)
        raise
    after = source_tree(DEMO_ROOT)
    if before != after:
        failure_log = DEMO_ROOT / "output" / f"{run_id}.failed.log"
        failure_log.parent.mkdir(parents=True, exist_ok=True)
        failure_log.write_bytes(output)
        raise ValueError(
            "Relevant source changed while tests/scenario ran; no evidence was published"
        )
    log = output.decode("utf-8")
    tests = parse_test_result(log)
    artifacts = load_artifacts(run_directory)
    verification = validate_run(artifacts, run_id)
    runtime = {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "sqlite": sqlite3.sqlite_version,
        "python_executable": sys.executable,
    }
    with tempfile.TemporaryDirectory(prefix="festive-result-evidence-") as temporary:
        staging = Path(temporary)
        for relative in artifacts:
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(run_directory / relative, target)
        (staging / "syntax_check.json").write_text(
            json.dumps(syntax, indent=2) + "\n", encoding="utf-8"
        )
        (staging / "run.log").write_bytes(output)
        file_hashes = {
            path.relative_to(staging).as_posix(): {
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size_bytes": path.stat().st_size,
            }
            for path in sorted(staging.rglob("*"))
            if path.is_file()
        }
        manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "captured_at_utc": captured_at,
            "capture_started_at_utc": started_at,
            "capture_duration_seconds": time.perf_counter() - start,
            "command": command,
            "working_directory": str(DEMO_ROOT),
            "exit_code": completed.returncode,
            "runtime": runtime,
            "test_result": tests,
            "verification": verification,
            "syntax_check": {
                "artifact": "syntax_check.json",
                "status": syntax["status"],
                "checked_count": syntax["checked_count"],
            },
            "source": after,
            "artifacts": file_hashes,
            "boundary": boundaries,
        }
        (staging / "evidence_manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        _publish_evidence(staging, destination, replace)
    if render:
        from .results_render import render_results

        render_results(destination, destination.parent)
    if snapshots:
        from .capture_snapshots import capture_snapshots

        capture_snapshots(destination.parent, replace=replace)
    return manifest


def main() -> int:
    """Capture new evidence or verify an existing run without replaying external effects."""
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run-id")
    mode.add_argument("--verify-run", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--no-render", action="store_true")
    parser.add_argument(
        "--snapshots",
        action="store_true",
        help="Capture rendered result pages with an installed local browser",
    )
    args = parser.parse_args()
    try:
        if args.verify_run is not None:
            if args.destination is not None or args.replace or args.no_render or args.snapshots:
                parser.error(
                    "--verify-run cannot be combined with capture/destination/render options"
                )
            print(json.dumps(verify_saved_run(args.verify_run.resolve()), indent=2))
            print(
                "Read-only saved-output verification; no tests, payment dispatch or cloud action was performed."
            )
        else:
            if args.no_render and args.snapshots:
                parser.error("--snapshots requires result rendering; remove --no-render")
            run_id = args.run_id or datetime.now(timezone.utc).strftime(
                "evidence_%Y%m%dT%H%M%S_%fZ"
            )
            destination = (
                args.destination or DEMO_ROOT / "docs" / "results" / "evidence"
            ).resolve()
            manifest = record(run_id, destination, args.replace, not args.no_render, args.snapshots)
            print(
                f"Recorded application tests: {format_test_result(manifest['test_result'])}. Verified {manifest['verification']['canonical_hashes_verified']} canonical exports."
            )
            print(f"Evidence: {destination}")
        return 0
    except (ValueError, KeyError, OSError, StopIteration, SyntaxError) as exc:
        print(f"Evidence capture/verification failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
