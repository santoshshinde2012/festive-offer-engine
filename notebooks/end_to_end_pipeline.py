# Databricks notebook source
# MAGIC %md
# MAGIC # Festive offers: prepare, publish, and reconcile
# MAGIC
# MAGIC This optional cloud integration uses the same publication validator and
# MAGIC ranking kernel as the local application. It preserves the source, quarantines
# MAGIC incomplete contracts, persists approved revisions, and exports a verified
# MAGIC serving bundle. A later run can ingest the local application's exported
# MAGIC operational records for analysis.
# MAGIC
# MAGIC Run only with the synthetic fixture in a dedicated demo schema. This notebook
# MAGIC does not run a SQLite ledger in Databricks or deploy a live payment service.
# MAGIC Cloud execution has not been performed by this authoring workflow; local
# MAGIC checks do not establish Databricks permissions, storage behavior, or sync SLAs.
# MAGIC See `../docs/databricks.md` for configuration and component boundaries.

# COMMAND ----------
from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pyspark.sql import DataFrame
from pyspark.sql import functions as F
from pyspark.sql import types as T

dbutils.widgets.text("repo_root", "", "Absolute Workspace path to companion code directory")
dbutils.widgets.text("catalog", "", "Writable Unity Catalog catalog")
dbutils.widgets.text("publication_id", "", "Unique publication ID; blank generates one")
dbutils.widgets.text(
    "business_revision", "", "Immutable serving revision; blank uses publication ID"
)
dbutils.widgets.text("export_volume", "", "/Volumes/catalog/schema/existing_volume")
dbutils.widgets.text(
    "operational_export_dir", "", "Optional /Volumes/... application export directory"
)
dbutils.widgets.text("ingestion_id", "", "Unique operational import ID; blank generates one")
dbutils.widgets.dropdown("publish_contracts", "true", ["true", "false"])

demo_root = Path(dbutils.widgets.get("repo_root"))
if not demo_root.is_absolute() or not (demo_root / "data" / "dataset.json").is_file():
    raise ValueError("repo_root must be an absolute Workspace path containing data/dataset.json")
sys.path.insert(0, str(demo_root / "src"))

if (
    dbutils.widgets.get("publish_contracts") == "false"
    and not dbutils.widgets.get("operational_export_dir").strip()
):
    raise ValueError("An import-only run requires operational_export_dir; no import was performed")

from festive_offers.online.operational_export import EXPORT_TABLES
from festive_offers.online.publication import prepare_bundle, verify_bundle
from festive_offers.pipeline import run_batch
from festive_offers.serialization import dataset_from_dict


def identifier(value: str, field: str) -> str:
    """Keep SQL identifiers and output directory names unambiguous."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(
            f"{field} must use letters, numbers and underscores and start with a letter/underscore"
        )
    return value


def volume_path(value: str, field: str) -> Path:
    """Limit artifacts to an explicitly configured, existing Unity Catalog volume."""
    path = Path(value)
    if (
        len(path.parts) < 5
        or path.parts[1] != "Volumes"
        or any(p in {".", ".."} for p in value.split("/"))
    ):
        raise ValueError(f"{field} must be an absolute /Volumes/catalog/schema/volume path")
    if not path.is_dir():
        raise ValueError(f"{field} must already exist and be accessible: {path}")
    return path


def now_id(prefix: str) -> str:
    return datetime.now(timezone.utc).strftime(f"{prefix}_%Y%m%dT%H%M%S_%fZ")


catalog = identifier(dbutils.widgets.get("catalog"), "catalog")
schema_name = f"`{catalog}`.`festive_offer_e2e`"
publication_id = identifier(
    dbutils.widgets.get("publication_id") or now_id("publication"), "publication_id"
)
revision = dbutils.widgets.get("business_revision") or publication_id
if not revision or len(revision) > 200:
    raise ValueError("business_revision must contain 1 to 200 characters")
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {schema_name}")

S, L, B = T.StringType(), T.LongType(), T.BooleanType()


def frame(rows: list[dict[str, Any]], fields: list[tuple[str, T.DataType, bool]]) -> DataFrame:
    """Supply schemas even for completely empty approved or operational results."""
    return spark.createDataFrame(rows, T.StructType([T.StructField(*field) for field in fields]))


def append_rows(
    table: str, rows: list[dict[str, Any]], fields: list[tuple[str, T.DataType, bool]]
) -> None:
    frame(rows, fields).write.format("delta").mode("append").saveAsTable(f"{schema_name}.`{table}`")


def refuse_existing(table: str, column: str, value: str) -> None:
    """Protect sequential demo reruns; this check is not a distributed lock."""
    name = f"{schema_name}.`{table}`"
    if (
        spark.catalog.tableExists(name)
        and spark.table(name).where(F.col(column) == value).limit(1).count()
    ):
        raise ValueError(f"{column}={value!r} already has data in {table}; choose a fresh ID")


def canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# COMMAND ----------
# MAGIC %md
# MAGIC ## Preserve source → validate → quarantine
# MAGIC
# MAGIC The source is the repository's deliberately small JSON fixture. The shared
# MAGIC `prepare_bundle` routine supplies the contract-quality decisions; this notebook
# MAGIC does not maintain a second set of eligibility rules.

# COMMAND ----------
if dbutils.widgets.get("publish_contracts") == "true":
    export_volume = volume_path(dbutils.widgets.get("export_volume"), "export_volume")
    fixture_path = demo_root / "data" / "dataset.json"
    if fixture_path.stat().st_size > 2_000_000:
        raise ValueError("This driver demonstration accepts source fixtures up to 2 MB")
    raw = json.loads(fixture_path.read_text(encoding="utf-8"))
    if raw.get("synthetic") is not True:
        raise ValueError("Use the explicitly labeled synthetic fixture only")
    bundle = prepare_bundle(raw, revision)
    verify_bundle(bundle)
    manifest = bundle["manifest"]

    for table in (
        "publication_sources",
        "raw_contracts",
        "approved_contracts",
        "quarantined_contracts",
        "publication_decisions",
        "publication_metrics",
        "publication_manifest",
    ):
        refuse_existing(table, "publication_id", publication_id)
    refuse_existing("publication_manifest", "revision", revision)
    output_dir = export_volume / publication_id
    if output_dir.exists():
        raise ValueError(f"Artifact directory already exists: {output_dir}")

    # Keep the exact source for replay, including assumptions and test requests.
    source_json = canonical(raw)
    append_rows(
        "publication_sources",
        [
            {
                "publication_id": publication_id,
                "revision": revision,
                "source_json": source_json,
                "source_sha256": digest(source_json),
            }
        ],
        [
            ("publication_id", S, False),
            ("revision", S, False),
            ("source_json", S, False),
            ("source_sha256", S, False),
        ],
    )

    append_rows(
        "raw_contracts",
        [
            {
                "publication_id": publication_id,
                "source_index": index,
                "offer_id": str(offer.get("offer_id", "")),
                "source_record_json": canonical(offer),
            }
            for index, offer in enumerate(raw["offers"])
        ],
        [
            ("publication_id", S, False),
            ("source_index", L, False),
            ("offer_id", S, False),
            ("source_record_json", S, False),
        ],
    )
    append_rows(
        "quarantined_contracts",
        [
            {
                "publication_id": publication_id,
                "revision": revision,
                "source_index": row["source_index"],
                "offer_id": row["offer_id"],
                "reason": row["reason"],
                "source_record_json": canonical(row["source_record"]),
            }
            for row in bundle["quarantined"]
        ],
        [
            ("publication_id", S, False),
            ("revision", S, False),
            ("source_index", L, False),
            ("offer_id", S, True),
            ("reason", S, False),
            ("source_record_json", S, False),
        ],
    )
    append_rows(
        "approved_contracts",
        [
            {
                "publication_id": publication_id,
                "revision": revision,
                "offer_id": offer["offer_id"],
                "catalog_version": offer["catalog_version"],
                "merchant": offer["merchant"],
                "starts_at": offer["starts_at"],
                "ends_at": offer["ends_at"],
                "contract_json": canonical(offer),
            }
            for offer in bundle["offers"]
        ],
        [
            ("publication_id", S, False),
            ("revision", S, False),
            ("offer_id", S, False),
            ("catalog_version", S, False),
            ("merchant", S, False),
            ("starts_at", S, False),
            ("ends_at", S, False),
            ("contract_json", S, False),
        ],
    )

    # Runtime wallets and baskets are not part of the approved serving contract.
    approved_inputs = {**raw, "offers": bundle["offers"], "assumptions": bundle["assumptions"]}
    result = run_batch(dataset_from_dict(approved_inputs), publication_id)
    decision_fields = [
        ("run_id", S, False),
        ("request_id", S, False),
        ("user_id", S, False),
        ("offer_id", S, False),
        ("issuer", S, False),
        ("catalog_version", S, False),
        ("eligible", B, False),
        ("rejection_reason", S, True),
        ("instrument_id", S, True),
        ("eligible_spend_paise", L, False),
        ("nominal_benefit_paise", L, False),
        ("expected_benefit_paise", L, False),
        ("realization_probability", S, True),
        ("payout", S, False),
        ("rank", L, True),
    ]
    decisions = [
        {"publication_id": publication_id, "revision": revision, **asdict(row)}
        for row in result.decisions
    ]
    append_rows(
        "publication_decisions",
        decisions,
        [("publication_id", S, False), ("revision", S, False), *decision_fields],
    )
    append_rows(
        "publication_metrics",
        [
            {
                "publication_id": publication_id,
                "revision": revision,
                "metrics_json": canonical(result.metrics),
            }
        ],
        [("publication_id", S, False), ("revision", S, False), ("metrics_json", S, False)],
    )

    # Export then read back the exact application contract before marking publication complete.
    output_dir.mkdir(parents=True, exist_ok=False)
    bundle_path = output_dir / "serving_bundle.json"
    bundle_path.write_text(canonical(bundle) + "\n", encoding="utf-8")
    verify_bundle(json.loads(bundle_path.read_text(encoding="utf-8")))
    (output_dir / "source_dataset.json").write_text(source_json + "\n", encoding="utf-8")
    (output_dir / "synthetic_client_inputs.json").write_text(
        canonical(
            {
                "synthetic": True,
                "wallets": raw["wallets"],
                "checkouts": raw["checkouts"],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    # Different Delta tables and the volume do not share an atomic transaction.
    # Consumers select only complete manifest rows; native application activation is separate.
    append_rows(
        "publication_manifest",
        [
            {
                "publication_id": publication_id,
                "revision": revision,
                "schema_version": manifest["schema_version"],
                "scoring_version": manifest["scoring_version"],
                "source_count": manifest["source_count"],
                "approved_count": manifest["approved_count"],
                "quarantine_count": manifest["quarantine_count"],
                "content_sha256": manifest["content_sha256"],
                "bundle_json": canonical(bundle),
                "bundle_volume_path": str(bundle_path),
                "published_at_utc": datetime.now(timezone.utc).isoformat(),
            }
        ],
        [
            ("publication_id", S, False),
            ("revision", S, False),
            ("schema_version", L, False),
            ("scoring_version", S, False),
            ("source_count", L, False),
            ("approved_count", L, False),
            ("quarantine_count", L, False),
            ("content_sha256", S, False),
            ("bundle_json", S, False),
            ("bundle_volume_path", S, False),
            ("published_at_utc", S, False),
        ],
    )
    print(f"Published approved synthetic revision {revision!r}: {bundle_path}")
    display(
        spark.table(f"{schema_name}.`publication_manifest`").where(
            F.col("publication_id") == publication_id
        )
    )
    display(
        spark.table(f"{schema_name}.`publication_decisions`")
        .where(F.col("publication_id") == publication_id)
        .orderBy("request_id", "rank")
    )

# COMMAND ----------
# MAGIC %md
# MAGIC ## Import operational evidence from the local reference application
# MAGIC
# MAGIC After downloading the serving bundle and running the local end-to-end demo,
# MAGIC upload its exported JSON files to a separate volume directory. Set
# MAGIC `operational_export_dir`, choose a fresh `ingestion_id`, and rerun with
# MAGIC `publish_contracts=false`. These are delayed analytical copies, not the
# MAGIC authoritative balances used to admit a checkout.

# COMMAND ----------
operational_dir_text = dbutils.widgets.get("operational_export_dir")
if operational_dir_text:
    operational_dir = volume_path(operational_dir_text, "operational_export_dir")
    ingestion_id = identifier(
        dbutils.widgets.get("ingestion_id") or now_id("ingestion"), "ingestion_id"
    )
    for table in ("operational_records", "operational_imports"):
        refuse_existing(table, "ingestion_id", ingestion_id)

    # The exporter takes all table files in one SQLite transaction. Reports have
    # a separately declared pre-export scope. This batch import is not CDC.
    export_manifest_path = operational_dir / "export_manifest.json"
    if not export_manifest_path.is_file() or export_manifest_path.stat().st_size > 100_000:
        raise ValueError("Expected the local export_manifest.json of at most 100 KB")
    export_manifest = json.loads(export_manifest_path.read_text(encoding="utf-8"))
    if export_manifest.get("schema_version") != 1:
        raise ValueError("Unsupported operational export schema")
    canonical_hashes = export_manifest["canonical_json_sha256"]
    if any(f"{table}.json" not in canonical_hashes for table in EXPORT_TABLES):
        raise ValueError("Operational export manifest is missing a required table")
    records: list[dict[str, Any]] = []
    file_hashes: dict[str, str] = {}
    row_counts: dict[str, int] = {}
    for source_table in EXPORT_TABLES:
        path = operational_dir / f"{source_table}.json"
        if not path.is_file() or path.stat().st_size > 5_000_000:
            raise ValueError(f"Expected a JSON array export of at most 5 MB: {path}")
        content = path.read_text(encoding="utf-8")
        rows = json.loads(content)
        if (
            not isinstance(rows, list)
            or len(rows) > 10_000
            or any(not isinstance(row, dict) for row in rows)
        ):
            raise ValueError(f"Expected at most 10,000 object rows in {path.name}")
        if digest(canonical(rows)) != canonical_hashes[path.name]:
            raise ValueError(f"Incomplete or modified operational export: {path.name}")
        file_hashes[path.name] = digest(content)
        row_counts[source_table] = len(rows)
        for index, row in enumerate(rows):
            records.append(
                {
                    "ingestion_id": ingestion_id,
                    "source_table": source_table,
                    "record_index": index,
                    "payload_json": canonical(row),
                    "source_sha256": file_hashes[path.name],
                }
            )

    append_rows(
        "operational_records",
        records,
        [
            ("ingestion_id", S, False),
            ("source_table", S, False),
            ("record_index", L, False),
            ("payload_json", S, False),
            ("source_sha256", S, False),
        ],
    )
    # The completion marker is last so SQL consumers exclude partial imports.
    append_rows(
        "operational_imports",
        [
            {
                "ingestion_id": ingestion_id,
                "source_directory": str(operational_dir),
                "source_hashes_json": canonical(file_hashes),
                "row_counts_json": canonical(row_counts),
                "export_manifest_json": canonical(export_manifest),
                "imported_at_utc": datetime.now(timezone.utc).isoformat(),
            }
        ],
        [
            ("ingestion_id", S, False),
            ("source_directory", S, False),
            ("source_hashes_json", S, False),
            ("row_counts_json", S, False),
            ("export_manifest_json", S, False),
            ("imported_at_utc", S, False),
        ],
    )
    print(f"Imported operational evidence: {ingestion_id}")
    display(
        spark.table(f"{schema_name}.`operational_records`")
        .where(F.col("ingestion_id") == ingestion_id)
        .groupBy("source_table")
        .count()
    )
