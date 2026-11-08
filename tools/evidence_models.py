"""Stable capture identities and the loaded context used by report rendering."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, TypedDict


class SourceTree(TypedDict):
    """Bind relative source names to their exact captured byte identities."""

    tree_sha256: str
    files: dict[str, str]
    algorithm: str


class CheckedPython(TypedDict):
    """Record a successful syntax check without claiming source execution."""

    path: str
    sha256: str
    status: Literal["OK"]


class SyntaxCheck(TypedDict):
    """Bind successful Python compilation to one source tree and interpreter."""

    schema_version: Literal[1]
    status: Literal["OK"]
    python: str
    implementation: str
    source_tree_sha256: str
    checked_count: int
    checked_files: list[CheckedPython]
    boundary: str


class CaptureBoundaries(TypedDict):
    """Separate a local scenario from optional test requests and unrun cloud work."""

    execution: Literal["local SQLite + fake provider"]
    scenario_store: Literal["SQLite"]
    databricks_workspace_executed: Literal[False]
    lakebase_cloud_executed: Literal[False]
    real_provider_executed: Literal[False]
    postgresql_tests_requested: bool
    postgresql_test_evidence: Literal["Per-test statuses are recorded in run.log"]


@dataclass(frozen=True)
class ReportData:
    """Hold one loaded evidence set so pure rendering cannot reread changed files."""

    manifest: Mapping[str, Any]
    summary: Mapping[str, Any]
    replay: Mapping[str, Any]
    slate: tuple[Mapping[str, Any], ...]
    metrics: Mapping[str, Any]
    bundle: Mapping[str, Any]
    log: str
    evidence_manifest_sha256: str
