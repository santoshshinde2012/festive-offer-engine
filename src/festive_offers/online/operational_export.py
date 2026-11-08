"""Write the shared, engine-independent operational evidence format."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

from .models import canonical
from .ports import Record

EXPORT_TABLES = (
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


@dataclass(frozen=True)
class TableSnapshot:
    """Carry copied table rows after the database has established its snapshot."""

    columns: tuple[str, ...]
    rows: tuple[Record, ...]


class ExportManifest(TypedDict):
    """Describe completed, hash-bound evidence without implying report atomicity."""

    schema_version: int
    scope: str
    canonical_json_sha256: dict[str, str]


def write_operational_export(
    destination: Path,
    tables: Mapping[str, TableSnapshot],
    reports: Mapping[str, Any],
    *,
    scope: str,
) -> ExportManifest:
    """Validate artifact identities before writing a new complete evidence set.

    Database adapters own snapshot consistency and copy their rows before calling
    this filesystem boundary. The manifest is written last. A failed write has
    no completion marker and must not be consumed as a completed export.
    """
    if set(tables) != set(EXPORT_TABLES):
        raise ValueError("An operational export requires exactly the 13 declared tables")
    for name in reports:
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[a-z_][a-z0-9_]{0,99}", name)
            or name in EXPORT_TABLES
            or name == "export_manifest"
        ):
            raise ValueError("Report export name must be a distinct safe identifier")

    # Encode before creating output: malformed reports cannot leave misleading
    # table files or escape the destination through a path-like report name.
    table_json = {name: canonical(tables[name].rows) for name in EXPORT_TABLES}
    report_json = {name: canonical(value) for name, value in reports.items()}
    destination.mkdir(parents=True, exist_ok=False)
    hashes = {
        f"{name}.json": hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        for name, encoded in {**table_json, **report_json}.items()
    }
    for table in EXPORT_TABLES:
        snapshot = tables[table]
        (destination / f"{table}.json").write_text(table_json[table] + "\n", encoding="utf-8")
        with (destination / f"{table}.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=snapshot.columns)
            writer.writeheader()
            writer.writerows(snapshot.rows)
    for name, encoded in report_json.items():
        (destination / f"{name}.json").write_text(encoded + "\n", encoding="utf-8")
    manifest: ExportManifest = {
        "schema_version": 1,
        "scope": scope,
        "canonical_json_sha256": hashes,
    }
    (destination / "export_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest
