from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

from .operational_export import (
    EXPORT_TABLES,
    ExportManifest,
    TableSnapshot,
    write_operational_export,
)
from .ports import UnitOfWork

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots(revision TEXT PRIMARY KEY,bundle_json TEXT NOT NULL,funding_json TEXT NOT NULL,customer_limit INTEGER NOT NULL CHECK(typeof(customer_limit)='integer' AND customer_limit>0),status TEXT NOT NULL,prepared_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS active_snapshot(singleton INTEGER PRIMARY KEY CHECK(singleton=1),revision TEXT NOT NULL,activated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS campaign_control(offer_id TEXT PRIMARY KEY,enabled INTEGER NOT NULL CHECK(enabled IN(0,1)));
CREATE TABLE IF NOT EXISTS budgets(offer_id TEXT PRIMARY KEY,funding_paise INTEGER NOT NULL CHECK(typeof(funding_paise)='integer' AND funding_paise>=0),available_paise INTEGER NOT NULL CHECK(typeof(available_paise)='integer' AND available_paise>=0),held_paise INTEGER NOT NULL CHECK(typeof(held_paise)='integer' AND held_paise>=0),committed_paise INTEGER NOT NULL CHECK(typeof(committed_paise)='integer' AND committed_paise>=0),CHECK(funding_paise=available_paise+held_paise+committed_paise));
CREATE TABLE IF NOT EXISTS wallets(user_id TEXT PRIMARY KEY,instruments_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS checkouts(request_id TEXT PRIMARY KEY,payment_id TEXT UNIQUE NOT NULL,request_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS quotes(quote_id TEXT PRIMARY KEY,revision TEXT NOT NULL,payment_id TEXT NOT NULL,request_json TEXT NOT NULL,wallet_hash TEXT NOT NULL,decisions_json TEXT NOT NULL,created_at TEXT NOT NULL,expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS applications(application_id TEXT PRIMARY KEY,idempotency_key TEXT UNIQUE NOT NULL,fingerprint TEXT NOT NULL,payment_id TEXT UNIQUE NOT NULL,checkout_id TEXT UNIQUE NOT NULL,quote_id TEXT NOT NULL,offer_id TEXT NOT NULL,revision TEXT NOT NULL,user_id TEXT NOT NULL,binding_json TEXT NOT NULL,nominal_paise INTEGER NOT NULL CHECK(typeof(nominal_paise)='integer' AND nominal_paise>=0),state TEXT NOT NULL,payment_state TEXT NOT NULL,benefit_state TEXT NOT NULL,budget_state TEXT NOT NULL,payout TEXT NOT NULL,provider_reference TEXT UNIQUE NOT NULL,created_at TEXT NOT NULL,expires_at TEXT NOT NULL,last_sequence INTEGER NOT NULL DEFAULT 0,reason TEXT);
CREATE TABLE IF NOT EXISTS customer_usage(offer_id TEXT NOT NULL,user_id TEXT NOT NULL,held_count INTEGER NOT NULL CHECK(typeof(held_count)='integer' AND held_count>=0),committed_count INTEGER NOT NULL CHECK(typeof(committed_count)='integer' AND committed_count>=0),PRIMARY KEY(offer_id,user_id));
CREATE TABLE IF NOT EXISTS outbox(application_id TEXT PRIMARY KEY,provider_reference TEXT UNIQUE NOT NULL,state TEXT NOT NULL,claimed_at TEXT);
CREATE TABLE IF NOT EXISTS inbox(event_id TEXT PRIMARY KEY,payload_hash TEXT NOT NULL,provider_reference TEXT NOT NULL,kind TEXT NOT NULL,sequence INTEGER NOT NULL,disposition TEXT NOT NULL,payload_json TEXT NOT NULL,received_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS benefit_obligations(application_id TEXT PRIMARY KEY,offer_id TEXT NOT NULL,user_id TEXT NOT NULL,amount_paise INTEGER NOT NULL CHECK(typeof(amount_paise)='integer' AND amount_paise>=0),payout TEXT NOT NULL,state TEXT NOT NULL,owed_at TEXT NOT NULL,fulfilled_at TEXT);
CREATE TABLE IF NOT EXISTS events(event_sequence INTEGER PRIMARY KEY AUTOINCREMENT,application_id TEXT,kind TEXT NOT NULL,payload_json TEXT NOT NULL,occurred_at TEXT NOT NULL);
"""


class SQLiteStore:
    """Serialize local admission and create constrained tables for new databases.

    Existing tables are preserved by CREATE TABLE IF NOT EXISTS. This reference
    does not automatically migrate old databases or rewrite retained evidence.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection:
            connection.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def rows(self, table: str) -> list[dict[str, Any]]:
        if table not in EXPORT_TABLES:
            raise ValueError("Unknown export table")
        with closing(self.connect()) as connection:
            return [dict(row) for row in connection.execute(f'SELECT * FROM "{table}"')]

    @contextmanager
    def unit_of_work(self) -> Iterator[UnitOfWork]:
        """Commit or roll back all semantic repositories as one local unit."""
        from .sqlite_repository import SQLiteUnitOfWork

        with self.transaction() as connection:
            yield SQLiteUnitOfWork(connection)

    def export(self, destination: Path, reports: Mapping[str, Any]) -> ExportManifest:
        """Copy one consistent SQLite snapshot, then write portable evidence."""
        tables: dict[str, TableSnapshot] = {}
        with self.transaction() as connection:
            for table in EXPORT_TABLES:
                cursor = connection.execute(
                    f'SELECT * FROM "{table}"'
                    + (" ORDER BY event_sequence" if table == "events" else "")
                )
                assert cursor.description is not None
                tables[table] = TableSnapshot(
                    columns=tuple(column[0] for column in cursor.description),
                    rows=tuple(dict(row) for row in cursor),
                )
        return write_operational_export(
            destination,
            tables,
            reports,
            scope="consistent local SQLite table snapshot; reports obtained before export",
        )
