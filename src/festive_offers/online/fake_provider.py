from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .models import PaymentBinding, ProviderMessage, canonical
from .security import sign

# This public test key authenticates only synthetic local messages. It is not a
# credential and must never be used by an actual payment integration.
SYNTHETIC_EVENT_KEY = b"public-local-fixture-key-not-for-real-payments"


class FakeProvider:
    """Persist fake effects separately so a retry can reuse the original reference."""

    def __init__(self, path: Path | str, event_key: bytes = SYNTHETIC_EVENT_KEY) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.event_key = event_key
        self.timeout_after_accept = False
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS payments(reference TEXT PRIMARY KEY,binding_json TEXT NOT NULL,submission_count INTEGER NOT NULL DEFAULT 1,last_event_json TEXT,sequence INTEGER NOT NULL DEFAULT 0)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS provider_events(reference TEXT NOT NULL,sequence INTEGER NOT NULL,payload_json TEXT NOT NULL,PRIMARY KEY(reference,sequence))"
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            # Binding checks and event sequence allocation include their reads
            # in the same serialized transaction as the corresponding writes.
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def submit(self, reference: str, binding: PaymentBinding) -> None:
        encoded = canonical(asdict(binding))
        with self._connect() as connection:
            previous = connection.execute(
                "SELECT binding_json FROM payments WHERE reference=?", (reference,)
            ).fetchone()
            if previous and previous["binding_json"] != encoded:
                raise ValueError("Provider reference reused for a different payment")
            connection.execute(
                "INSERT OR IGNORE INTO payments(reference,binding_json) VALUES (?,?)",
                (reference, encoded),
            )
        if self.timeout_after_accept:
            raise TimeoutError("Synthetic network timeout after provider accepted payment")

    def event(
        self,
        reference: str,
        kind: str,
        *,
        binding: PaymentBinding | None = None,
        benefit_paise: int | None = None,
        event_id: str | None = None,
    ) -> ProviderMessage:
        """Create authenticated synthetic evidence independently of application state."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM payments WHERE reference=?", (reference,)
            ).fetchone()
            if row is None:
                raise ValueError("Fake provider has no such payment")
            sequence = row["sequence"] + 1
            payload = {
                "event_id": event_id or f"{reference}:{sequence}",
                "provider_reference": reference,
                "kind": kind,
                "sequence": sequence,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "binding": asdict(binding) if binding else json.loads(row["binding_json"]),
                "benefit_paise": benefit_paise,
            }
            connection.execute(
                "UPDATE payments SET last_event_json=?,sequence=? WHERE reference=?",
                (canonical(payload), sequence, reference),
            )
            connection.execute(
                "INSERT INTO provider_events VALUES (?,?,?)",
                (reference, sequence, canonical(payload)),
            )
        return ProviderMessage(payload, sign(payload, self.event_key))

    def lookup(self, reference: str) -> tuple[ProviderMessage, ...]:
        """Return durable payment and benefit evidence, not just the last callback."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM provider_events WHERE reference=? ORDER BY sequence",
                (reference,),
            ).fetchall()
        return tuple(
            ProviderMessage(json.loads(row[0]), sign(json.loads(row[0]), self.event_key))
            for row in rows
        )

    def payment_count(self) -> int:
        with self._connect() as connection:
            return connection.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
