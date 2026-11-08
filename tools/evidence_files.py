"""Read bounded evidence and capture source bytes without executing the source."""

from __future__ import annotations

import hashlib
import json
import platform
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from .evidence_models import CheckedPython, ReportData, SourceTree, SyntaxCheck

MAX_JSON_BYTES = 2 * 1024 * 1024
SOURCE_ALGORITHM = "SHA-256 of canonical JSON mapping relative source paths to byte SHA-256"


def _source_digest(files: Mapping[str, str]) -> str:
    encoded = json.dumps(files, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _inside(root: Path, relative: str, label: str) -> Path:
    path = PurePosixPath(relative)
    if path.is_absolute() or not path.parts or ".." in path.parts or "\\" in relative:
        raise ValueError(f"Unsafe {label} path: {relative}")
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"{label} path escapes its folder: {relative}")
    return resolved


def source_tree(root: Path) -> SourceTree:
    """Bind executable/configuration inputs while excluding private generated files."""
    root = root.resolve()
    files: dict[str, str] = {}
    candidates = list(root.glob("*.sh"))
    for pattern in ("requirements*.txt", "*.yml", "*.yaml", "*.toml"):
        candidates.extend(root.glob(pattern))
    extensions = {
        ".py",
        ".sh",
        ".sql",
        ".js",
        ".cjs",
        ".mjs",
        ".css",
        ".html",
        ".yml",
        ".yaml",
        ".json",
        ".svg",
    }
    excluded = {"output", ".venv", "__pycache__", "node_modules", ".git"}
    for directory in ("src", "tests", "notebooks", "sql", "tools", "web", "deployment"):
        candidates.extend(
            path
            for path in (root / directory).rglob("*")
            if path.suffix in extensions and not excluded.intersection(path.relative_to(root).parts)
        )
    candidates.extend(
        path
        for path in (root / "data").rglob("*.json")
        if not excluded.intersection(path.relative_to(root).parts)
    )
    for candidate in sorted(set(candidates)):
        relative = candidate.relative_to(root).as_posix()
        path = _inside(root, relative, "Source")
        if path.is_file():
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"tree_sha256": _source_digest(files), "files": files, "algorithm": SOURCE_ALGORITHM}


def compile_sources(root: Path, source: SourceTree) -> SyntaxCheck:
    """Compile exact captured Python bytes without imports, execution or pycache."""
    root = root.resolve()
    if _source_digest(source["files"]) != source["tree_sha256"]:
        raise ValueError("Source tree digest is internally inconsistent")
    checked: list[CheckedPython] = []
    for relative, expected_hash in sorted(source["files"].items()):
        path = _inside(root, relative, "Source")
        if not relative.endswith(".py"):
            continue
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected_hash:
            raise ValueError(f"Source changed before syntax checking: {relative}")
        compile(content, relative, "exec", dont_inherit=True)
        checked.append({"path": relative, "sha256": expected_hash, "status": "OK"})
    return {
        "schema_version": 1,
        "status": "OK",
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "source_tree_sha256": source["tree_sha256"],
        "checked_count": len(checked),
        "checked_files": checked,
        "boundary": "Python syntax compilation only; no source execution, imports or Databricks workspace run",
    }


def _json_bytes(root: Path, relative: str) -> bytes:
    path = _inside(root, relative, "Result artifact")
    if path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError(f"Result JSON exceeds the small-fixture evidence bound: {relative}")
    content = path.read_bytes()
    if len(content) > MAX_JSON_BYTES:
        raise ValueError(f"Result JSON exceeds the small-fixture evidence bound: {relative}")
    return content


def load_artifacts(run_directory: Path) -> dict[str, Any]:
    """Read raw bounded JSON without opening or initializing application databases."""
    if not run_directory.is_dir():
        raise ValueError("The requested run directory does not exist")
    root = run_directory.resolve()
    return {
        path.relative_to(root).as_posix(): json.loads(
            _json_bytes(root, path.relative_to(root).as_posix()).decode("utf-8")
        )
        for path in sorted(root.rglob("*.json"))
    }


def load_report_data(evidence_dir: Path) -> ReportData:
    """Read each report input once; semantic evidence checks remain the caller's job."""
    root = evidence_dir.resolve()
    manifest_bytes = _json_bytes(root, "evidence_manifest.json")

    def object_value(content: bytes, relative: str) -> dict[str, Any]:
        raw: Any = json.loads(content.decode("utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"Expected a report JSON object: {relative}")
        return raw

    def object_file(relative: str) -> dict[str, Any]:
        return object_value(_json_bytes(root, relative), relative)

    slate: Any = json.loads(_json_bytes(root, "batch/slate.json").decode("utf-8"))
    if not isinstance(slate, list) or any(not isinstance(row, dict) for row in slate):
        raise ValueError("Expected a report JSON array of slate objects")
    return ReportData(
        manifest=object_value(manifest_bytes, "evidence_manifest.json"),
        summary=object_file("scenario_summary.json"),
        replay=object_file("exports/replay_report.json"),
        slate=tuple(slate),
        metrics=object_file("batch/metrics.json"),
        bundle=object_file("serving_bundle.json"),
        log=_inside(root, "run.log", "Result artifact").read_bytes().decode("utf-8"),
        evidence_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
    )
