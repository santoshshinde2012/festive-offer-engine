"""SQLite implementations of the operational repository ports."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, cast

from .models import canonical
from .ports import (
    ActiveSnapshot,
    ApplicationRecord,
    CheckoutRecord,
    QuoteRecord,
    Record,
    SnapshotRecord,
)


class SnapshotRepository:
    """Persist complete publications and stable campaign funding in one transaction."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def get(self, revision: str) -> SnapshotRecord | None:
        row = self.connection.execute(
            "SELECT * FROM snapshots WHERE revision=?", (revision,)
        ).fetchone()
        return cast(SnapshotRecord | None, dict(row) if row else None)

    def active(self) -> ActiveSnapshot | None:
        row = self.connection.execute(
            "SELECT a.revision,a.activated_at,s.bundle_json,s.customer_limit,s.prepared_at FROM active_snapshot a JOIN snapshots s ON s.revision=a.revision WHERE a.singleton=1"
        ).fetchone()
        return cast(ActiveSnapshot | None, dict(row) if row else None)

    def prepare(self, record: SnapshotRecord) -> None:
        self.connection.execute(
            "INSERT INTO snapshots VALUES (:revision,:bundle_json,:funding_json,:customer_limit,:status,:prepared_at)",
            record,
        )

    def funding(self, offer_id: str) -> int | None:
        row = self.connection.execute(
            "SELECT funding_paise FROM budgets WHERE offer_id=?", (offer_id,)
        ).fetchone()
        return row[0] if row else None

    def initialize_campaign(self, offer_id: str, funding_paise: int) -> None:
        self.connection.execute(
            "INSERT INTO budgets VALUES (?,?,?,0,0) ON CONFLICT(offer_id) DO NOTHING",
            (offer_id, funding_paise, funding_paise),
        )
        self.connection.execute(
            "INSERT INTO campaign_control VALUES (?,1) ON CONFLICT(offer_id) DO NOTHING",
            (offer_id,),
        )

    def activate(self, revision: str, now: str) -> None:
        self.connection.execute(
            "INSERT INTO active_snapshot VALUES (1,?,?) ON CONFLICT(singleton) DO UPDATE SET revision=excluded.revision,activated_at=excluded.activated_at",
            (revision, now),
        )
        self.connection.execute(
            "UPDATE snapshots SET status='ACTIVE' WHERE revision=?", (revision,)
        )

    def set_enabled(self, offer_id: str, enabled: bool) -> bool:
        return bool(
            self.connection.execute(
                "UPDATE campaign_control SET enabled=? WHERE offer_id=?", (int(enabled), offer_id)
            ).rowcount
        )


class CheckoutRepository:
    """Persist trusted merchant inputs, verified wallets and original quote terms."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def wallet(self, user_id: str) -> list[Record]:
        row = self.connection.execute(
            "SELECT instruments_json FROM wallets WHERE user_id=?", (user_id,)
        ).fetchone()
        return json.loads(row[0]) if row else []

    def save_wallet(self, user_id: str, instruments: list[Record]) -> None:
        self.connection.execute(
            "INSERT INTO wallets VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET instruments_json=excluded.instruments_json",
            (user_id, canonical(instruments)),
        )

    def checkout(self, request_id: str) -> CheckoutRecord | None:
        row = self.connection.execute(
            "SELECT * FROM checkouts WHERE request_id=?", (request_id,)
        ).fetchone()
        return cast(CheckoutRecord | None, dict(row) if row else None)

    def save_checkout(self, record: CheckoutRecord) -> None:
        self.connection.execute(
            "INSERT INTO checkouts VALUES (:request_id,:payment_id,:request_json) ON CONFLICT(request_id) DO UPDATE SET payment_id=excluded.payment_id,request_json=excluded.request_json",
            record,
        )

    def is_admitted(self, request_id: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM applications WHERE checkout_id=? AND budget_state!='RELEASED'",
                (request_id,),
            ).fetchone()
            is not None
        )

    def quote(self, quote_id: str) -> QuoteRecord | None:
        row = self.connection.execute(
            "SELECT * FROM quotes WHERE quote_id=?", (quote_id,)
        ).fetchone()
        return cast(QuoteRecord | None, dict(row) if row else None)

    def save_quote(self, record: QuoteRecord) -> None:
        self.connection.execute(
            "INSERT INTO quotes VALUES (:quote_id,:revision,:payment_id,:request_json,:wallet_hash,:decisions_json,:created_at,:expires_at)",
            record,
        )

    def enabled_offer_ids(self) -> set[str]:
        return {
            row[0]
            for row in self.connection.execute(
                "SELECT offer_id FROM campaign_control WHERE enabled=1"
            )
        }


class AdmissionRepository:
    """Reserve funds and create the application/usage/outbox aggregate without committing."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def by_key(self, key: str) -> ApplicationRecord | None:
        row = self.connection.execute(
            "SELECT * FROM applications WHERE idempotency_key=?", (key,)
        ).fetchone()
        return cast(ApplicationRecord | None, dict(row) if row else None)

    def has_payment(self, payment_id: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM applications WHERE payment_id=?", (payment_id,)
            ).fetchone()
            is not None
        )

    def has_checkout(self, checkout_id: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM applications WHERE checkout_id=?", (checkout_id,)
            ).fetchone()
            is not None
        )

    def usage(self, offer_id: str, user_id: str) -> tuple[int, int]:
        self.connection.execute(
            "INSERT INTO customer_usage VALUES (?,?,0,0) ON CONFLICT(offer_id,user_id) DO NOTHING",
            (offer_id, user_id),
        )
        row = self.connection.execute(
            "SELECT held_count,committed_count FROM customer_usage WHERE offer_id=? AND user_id=?",
            (offer_id, user_id),
        ).fetchone()
        return row[0], row[1]

    def reserve_budget(self, offer_id: str, amount_paise: int) -> bool:
        return bool(
            self.connection.execute(
                "UPDATE budgets SET available_paise=available_paise-?,held_paise=held_paise+? WHERE offer_id=? AND available_paise>=?",
                (amount_paise, amount_paise, offer_id, amount_paise),
            ).rowcount
        )

    def create(self, record: ApplicationRecord) -> None:
        fields = tuple(ApplicationRecord.__annotations__)
        self.connection.execute(
            f"INSERT INTO applications ({','.join(fields)}) VALUES ({','.join(':' + field for field in fields)})",
            record,
        )
        self.connection.execute(
            "UPDATE customer_usage SET held_count=held_count+1 WHERE offer_id=? AND user_id=?",
            (record["offer_id"], record["user_id"]),
        )
        self.connection.execute(
            "INSERT INTO outbox(application_id,provider_reference,state) VALUES (?,?,'READY')",
            (record["application_id"], record["provider_reference"]),
        )


class DispatchRepository:
    """Claim or release held work while retaining the caller's atomic boundary."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def get(self, application_id: str) -> ApplicationRecord | None:
        row = self.connection.execute(
            "SELECT * FROM applications WHERE application_id=?", (application_id,)
        ).fetchone()
        return cast(ApplicationRecord | None, dict(row) if row else None)

    def expired_ready(self, now: str) -> list[ApplicationRecord]:
        return cast(
            list[ApplicationRecord],
            [
                dict(row)
                for row in self.connection.execute(
                    "SELECT a.* FROM applications a JOIN outbox o USING(application_id) WHERE a.state='HELD' AND a.budget_state='HELD' AND o.state='READY' AND a.expires_at<=?",
                    (now,),
                )
            ],
        )

    def release(self, application: ApplicationRecord, reason: str) -> None:
        amount = application["nominal_paise"]
        self.connection.execute(
            "UPDATE budgets SET available_paise=available_paise+?,held_paise=held_paise-? WHERE offer_id=?",
            (amount, amount, application["offer_id"]),
        )
        self.connection.execute(
            "UPDATE customer_usage SET held_count=held_count-1 WHERE offer_id=? AND user_id=?",
            (application["offer_id"], application["user_id"]),
        )
        self.connection.execute(
            "UPDATE applications SET state='RELEASED',payment_state='FAILED',budget_state='RELEASED',reason=? WHERE application_id=?",
            (reason, application["application_id"]),
        )
        self.connection.execute(
            "UPDATE outbox SET state='CANCELLED' WHERE application_id=?",
            (application["application_id"],),
        )

    def claim(self, application_id: str, now: str) -> bool:
        changed = self.connection.execute(
            "UPDATE applications SET state='DISPATCHING',payment_state='IN_FLIGHT' WHERE application_id=? AND state='HELD'",
            (application_id,),
        ).rowcount
        claimed = self.connection.execute(
            "UPDATE outbox SET state='CLAIMED',claimed_at=? WHERE application_id=? AND state='READY'",
            (now, application_id),
        ).rowcount
        return bool(changed and claimed)

    def finish(
        self,
        application_id: str,
        state: str,
        payment_state: str,
        outbox_state: str,
        reason: str | None,
    ) -> None:
        self.connection.execute(
            "UPDATE applications SET state=?,payment_state=?,reason=? WHERE application_id=? AND state='DISPATCHING'",
            (state, payment_state, reason, application_id),
        )
        self.connection.execute(
            "UPDATE outbox SET state=? WHERE application_id=? AND state='CLAIMED'",
            (outbox_state, application_id),
        )


class EvidenceRepository:
    """Persist guarded payment evidence and benefit obligations in the caller's unit."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def by_reference(self, reference: str) -> ApplicationRecord | None:
        row = self.connection.execute(
            "SELECT * FROM applications WHERE provider_reference=?", (reference,)
        ).fetchone()
        return cast(ApplicationRecord | None, dict(row) if row else None)

    def inbox(self, event_id: str) -> Record | None:
        row = self.connection.execute(
            "SELECT * FROM inbox WHERE event_id=?", (event_id,)
        ).fetchone()
        return dict(row) if row else None

    def save_inbox(self, record: Record, existing: bool) -> None:
        if existing:
            self.connection.execute(
                "UPDATE inbox SET disposition=? WHERE event_id=?",
                (record["disposition"], record["event_id"]),
            )
        else:
            self.connection.execute(
                "INSERT INTO inbox VALUES (:event_id,:payload_hash,:provider_reference,:kind,:sequence,:disposition,:payload_json,:received_at)",
                record,
            )

    def mark_unknown(self, application_id: str, retained_reason: str) -> None:
        self.connection.execute(
            "UPDATE applications SET state='UNKNOWN',reason=? WHERE application_id=?",
            (retained_reason, application_id),
        )

    def qualify(self, application: ApplicationRecord, now: str) -> None:
        amount = application["nominal_paise"]
        self.connection.execute(
            "UPDATE budgets SET held_paise=held_paise-?,committed_paise=committed_paise+? WHERE offer_id=?",
            (amount, amount, application["offer_id"]),
        )
        self.connection.execute(
            "UPDATE customer_usage SET held_count=held_count-1,committed_count=committed_count+1 WHERE offer_id=? AND user_id=?",
            (application["offer_id"], application["user_id"]),
        )
        self.connection.execute(
            "UPDATE applications SET state='OWED',payment_state='CAPTURED',benefit_state='OWED',budget_state='COMMITTED',reason=NULL WHERE application_id=?",
            (application["application_id"],),
        )
        self.connection.execute(
            "INSERT INTO benefit_obligations VALUES (?,?,?,?,?,'OWED',?,NULL)",
            (
                application["application_id"],
                application["offer_id"],
                application["user_id"],
                amount,
                application["payout"],
                now,
            ),
        )

    def fulfill(
        self, application_id: str, state: str, retained_reason: str | None, now: str
    ) -> None:
        self.connection.execute(
            "UPDATE applications SET state=?,benefit_state='FULFILLED',reason=? WHERE application_id=?",
            (state, retained_reason, application_id),
        )
        self.connection.execute(
            "UPDATE benefit_obligations SET state='FULFILLED',fulfilled_at=? WHERE application_id=?",
            (now, application_id),
        )

    def advance_sequence(self, application_id: str, sequence: int) -> None:
        self.connection.execute(
            "UPDATE applications SET last_sequence=MAX(last_sequence,?) WHERE application_id=?",
            (sequence, application_id),
        )


class EventRepository:
    """Append journal records alongside the financial changes they describe."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def append(self, application_id: str | None, kind: str, payload: Any, now: str) -> None:
        self.connection.execute(
            "INSERT INTO events(application_id,kind,payload_json,occurred_at) VALUES (?,?,?,?)",
            (application_id, kind, canonical(payload), now),
        )


class ReportRepository:
    """Read a consistent replay snapshot without changing operational rows."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def snapshot(self) -> dict[str, list[Record]]:
        return {
            table: [
                dict(row)
                for row in self.connection.execute(
                    f"SELECT * FROM {table}"
                    + (" ORDER BY event_sequence" if table == "events" else "")
                )
            ]
            for table in ("budgets", "events", "applications", "benefit_obligations")
        }


class WorkQueueRepository:
    """Page recovery work without treating a missing event as payment failure."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def ready(self, limit: int) -> list[str]:
        return [
            row[0]
            for row in self.connection.execute(
                "SELECT a.application_id FROM applications a JOIN outbox o USING(application_id) "
                "WHERE a.state='HELD' AND a.budget_state='HELD' AND o.state='READY' "
                "ORDER BY a.application_id LIMIT ?",
                (limit,),
            )
        ]

    def unresolved(self, after: str, limit: int) -> list[str]:
        return [
            row[0]
            for row in self.connection.execute(
                "SELECT application_id FROM applications WHERE application_id>? AND "
                "(state IN ('DISPATCHING','PENDING','UNKNOWN') OR benefit_state='OWED') "
                "ORDER BY application_id LIMIT ?",
                (after, limit),
            )
        ]


class SQLiteUnitOfWork:
    """Bind all semantic repositories to the same already-open transaction."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.snapshots = SnapshotRepository(connection)
        self.checkouts = CheckoutRepository(connection)
        self.admission = AdmissionRepository(connection)
        self.dispatch = DispatchRepository(connection)
        self.evidence = EvidenceRepository(connection)
        self.events = EventRepository(connection)
        self.reports = ReportRepository(connection)
        self.work = WorkQueueRepository(connection)
