"""PostgreSQL repositories sharing the store's already-locked transaction.

The explicit SQL keeps this adapter independent of SQLite placeholder and
upsert behavior. Facets never commit; PostgreSQLStore owns that boundary.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from .models import canonical
from .ports import (
    ActiveSnapshot,
    ApplicationRecord,
    CheckoutRecord,
    QuoteRecord,
    Record,
    SnapshotRecord,
)

if TYPE_CHECKING:
    from psycopg import Connection


class SnapshotRepository:
    """Persist publications and stable campaign funding without committing independently."""

    def __init__(self, connection: Connection[Record]) -> None:
        self.connection = connection

    def get(self, revision: str) -> SnapshotRecord | None:
        return cast(
            SnapshotRecord | None,
            self.connection.execute(
                "SELECT * FROM snapshots WHERE revision=%s", (revision,)
            ).fetchone(),
        )

    def active(self) -> ActiveSnapshot | None:
        return cast(
            ActiveSnapshot | None,
            self.connection.execute(
                "SELECT a.revision,a.activated_at,s.bundle_json,s.customer_limit,s.prepared_at FROM active_snapshot a JOIN snapshots s ON s.revision=a.revision WHERE a.singleton=1"
            ).fetchone(),
        )

    def prepare(self, record: SnapshotRecord) -> None:
        self.connection.execute(
            "INSERT INTO snapshots(revision,bundle_json,funding_json,customer_limit,status,prepared_at) VALUES (%(revision)s,%(bundle_json)s,%(funding_json)s,%(customer_limit)s,%(status)s,%(prepared_at)s)",
            record,
        )

    def funding(self, offer_id: str) -> int | None:
        row = self.connection.execute(
            "SELECT funding_paise FROM budgets WHERE offer_id=%s", (offer_id,)
        ).fetchone()
        return row["funding_paise"] if row else None

    def initialize_campaign(self, offer_id: str, funding_paise: int) -> None:
        self.connection.execute(
            "INSERT INTO budgets(offer_id,funding_paise,available_paise,held_paise,committed_paise) VALUES (%s,%s,%s,0,0) ON CONFLICT(offer_id) DO NOTHING",
            (offer_id, funding_paise, funding_paise),
        )
        self.connection.execute(
            "INSERT INTO campaign_control(offer_id,enabled) VALUES (%s,1) ON CONFLICT(offer_id) DO NOTHING",
            (offer_id,),
        )

    def activate(self, revision: str, now: str) -> None:
        self.connection.execute(
            "INSERT INTO active_snapshot(singleton,revision,activated_at) VALUES (1,%s,%s) ON CONFLICT(singleton) DO UPDATE SET revision=excluded.revision,activated_at=excluded.activated_at",
            (revision, now),
        )
        self.connection.execute(
            "UPDATE snapshots SET status='ACTIVE' WHERE revision=%s", (revision,)
        )

    def set_enabled(self, offer_id: str, enabled: bool) -> bool:
        return bool(
            self.connection.execute(
                "UPDATE campaign_control SET enabled=%s WHERE offer_id=%s", (int(enabled), offer_id)
            ).rowcount
        )


class CheckoutRepository:
    """Retain trusted baskets, wallet facts and the original quote terms."""

    def __init__(self, connection: Connection[Record]) -> None:
        self.connection = connection

    def wallet(self, user_id: str) -> list[Record]:
        row = self.connection.execute(
            "SELECT instruments_json FROM wallets WHERE user_id=%s", (user_id,)
        ).fetchone()
        return json.loads(row["instruments_json"]) if row else []

    def save_wallet(self, user_id: str, instruments: list[Record]) -> None:
        self.connection.execute(
            "INSERT INTO wallets(user_id,instruments_json) VALUES (%s,%s) ON CONFLICT(user_id) DO UPDATE SET instruments_json=excluded.instruments_json",
            (user_id, canonical(instruments)),
        )

    def checkout(self, request_id: str) -> CheckoutRecord | None:
        return cast(
            CheckoutRecord | None,
            self.connection.execute(
                "SELECT * FROM checkouts WHERE request_id=%s", (request_id,)
            ).fetchone(),
        )

    def save_checkout(self, record: CheckoutRecord) -> None:
        self.connection.execute(
            "INSERT INTO checkouts(request_id,payment_id,request_json) VALUES (%(request_id)s,%(payment_id)s,%(request_json)s) ON CONFLICT(request_id) DO UPDATE SET payment_id=excluded.payment_id,request_json=excluded.request_json",
            record,
        )

    def is_admitted(self, request_id: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM applications WHERE checkout_id=%s AND budget_state!='RELEASED'",
                (request_id,),
            ).fetchone()
            is not None
        )

    def quote(self, quote_id: str) -> QuoteRecord | None:
        return cast(
            QuoteRecord | None,
            self.connection.execute(
                "SELECT * FROM quotes WHERE quote_id=%s", (quote_id,)
            ).fetchone(),
        )

    def save_quote(self, record: QuoteRecord) -> None:
        self.connection.execute(
            "INSERT INTO quotes(quote_id,revision,payment_id,request_json,wallet_hash,decisions_json,created_at,expires_at) VALUES (%(quote_id)s,%(revision)s,%(payment_id)s,%(request_json)s,%(wallet_hash)s,%(decisions_json)s,%(created_at)s,%(expires_at)s)",
            record,
        )

    def enabled_offer_ids(self) -> set[str]:
        return {
            row["offer_id"]
            for row in self.connection.execute(
                "SELECT offer_id FROM campaign_control WHERE enabled=1"
            )
        }


class AdmissionRepository:
    """Save held liability, customer usage and pending work as one aggregate."""

    def __init__(self, connection: Connection[Record]) -> None:
        self.connection = connection

    def by_key(self, key: str) -> ApplicationRecord | None:
        return cast(
            ApplicationRecord | None,
            self.connection.execute(
                "SELECT * FROM applications WHERE idempotency_key=%s", (key,)
            ).fetchone(),
        )

    def has_payment(self, payment_id: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM applications WHERE payment_id=%s", (payment_id,)
            ).fetchone()
            is not None
        )

    def has_checkout(self, checkout_id: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM applications WHERE checkout_id=%s", (checkout_id,)
            ).fetchone()
            is not None
        )

    def usage(self, offer_id: str, user_id: str) -> tuple[int, int]:
        self.connection.execute(
            "INSERT INTO customer_usage(offer_id,user_id,held_count,committed_count) VALUES (%s,%s,0,0) ON CONFLICT(offer_id,user_id) DO NOTHING",
            (offer_id, user_id),
        )
        row = self.connection.execute(
            "SELECT held_count,committed_count FROM customer_usage WHERE offer_id=%s AND user_id=%s",
            (offer_id, user_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("Customer usage row was not initialized")
        return row["held_count"], row["committed_count"]

    def reserve_budget(self, offer_id: str, amount_paise: int) -> bool:
        return bool(
            self.connection.execute(
                "UPDATE budgets SET available_paise=available_paise-%s,held_paise=held_paise+%s WHERE offer_id=%s AND available_paise>=%s",
                (amount_paise, amount_paise, offer_id, amount_paise),
            ).rowcount
        )

    def create(self, record: ApplicationRecord) -> None:
        fields = tuple(ApplicationRecord.__annotations__)
        # Column names come exclusively from the static TypedDict contract.
        placeholders = ",".join("%(" + field + ")s" for field in fields)
        self.connection.execute(
            f"INSERT INTO applications ({','.join(fields)}) VALUES ({placeholders})", record
        )
        self.connection.execute(
            "UPDATE customer_usage SET held_count=held_count+1 WHERE offer_id=%s AND user_id=%s",
            (record["offer_id"], record["user_id"]),
        )
        self.connection.execute(
            "INSERT INTO outbox(application_id,provider_reference,state) VALUES (%s,%s,'READY')",
            (record["application_id"], record["provider_reference"]),
        )


class DispatchRepository:
    """Claim or release work inside the caller-owned financial transaction."""

    def __init__(self, connection: Connection[Record]) -> None:
        self.connection = connection

    def get(self, application_id: str) -> ApplicationRecord | None:
        return cast(
            ApplicationRecord | None,
            self.connection.execute(
                "SELECT * FROM applications WHERE application_id=%s", (application_id,)
            ).fetchone(),
        )

    def expired_ready(self, now: str) -> list[ApplicationRecord]:
        return cast(
            list[ApplicationRecord],
            self.connection.execute(
                "SELECT a.* FROM applications a JOIN outbox o USING(application_id) WHERE a.state='HELD' AND a.budget_state='HELD' AND o.state='READY' AND a.expires_at<=%s",
                (now,),
            ).fetchall(),
        )

    def release(self, application: ApplicationRecord, reason: str) -> None:
        amount = application["nominal_paise"]
        self.connection.execute(
            "UPDATE budgets SET available_paise=available_paise+%s,held_paise=held_paise-%s WHERE offer_id=%s",
            (amount, amount, application["offer_id"]),
        )
        self.connection.execute(
            "UPDATE customer_usage SET held_count=held_count-1 WHERE offer_id=%s AND user_id=%s",
            (application["offer_id"], application["user_id"]),
        )
        self.connection.execute(
            "UPDATE applications SET state='RELEASED',payment_state='FAILED',budget_state='RELEASED',reason=%s WHERE application_id=%s",
            (reason, application["application_id"]),
        )
        self.connection.execute(
            "UPDATE outbox SET state='CANCELLED' WHERE application_id=%s",
            (application["application_id"],),
        )

    def claim(self, application_id: str, now: str) -> bool:
        changed = self.connection.execute(
            "UPDATE applications SET state='DISPATCHING',payment_state='IN_FLIGHT' WHERE application_id=%s AND state='HELD'",
            (application_id,),
        ).rowcount
        claimed = self.connection.execute(
            "UPDATE outbox SET state='CLAIMED',claimed_at=%s WHERE application_id=%s AND state='READY'",
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
            "UPDATE applications SET state=%s,payment_state=%s,reason=%s WHERE application_id=%s AND state='DISPATCHING'",
            (state, payment_state, reason, application_id),
        )
        self.connection.execute(
            "UPDATE outbox SET state=%s WHERE application_id=%s AND state='CLAIMED'",
            (outbox_state, application_id),
        )


class EvidenceRepository:
    """Persist guarded payment facts and separately fulfilled benefit obligations."""

    def __init__(self, connection: Connection[Record]) -> None:
        self.connection = connection

    def by_reference(self, reference: str) -> ApplicationRecord | None:
        return cast(
            ApplicationRecord | None,
            self.connection.execute(
                "SELECT * FROM applications WHERE provider_reference=%s", (reference,)
            ).fetchone(),
        )

    def inbox(self, event_id: str) -> Record | None:
        return self.connection.execute(
            "SELECT * FROM inbox WHERE event_id=%s", (event_id,)
        ).fetchone()

    def save_inbox(self, record: Record, existing: bool) -> None:
        if existing:
            self.connection.execute(
                "UPDATE inbox SET disposition=%s WHERE event_id=%s",
                (record["disposition"], record["event_id"]),
            )
        else:
            self.connection.execute(
                "INSERT INTO inbox(event_id,payload_hash,provider_reference,kind,sequence,disposition,payload_json,received_at) VALUES (%(event_id)s,%(payload_hash)s,%(provider_reference)s,%(kind)s,%(sequence)s,%(disposition)s,%(payload_json)s,%(received_at)s)",
                record,
            )

    def mark_unknown(self, application_id: str, retained_reason: str) -> None:
        self.connection.execute(
            "UPDATE applications SET state='UNKNOWN',reason=%s WHERE application_id=%s",
            (retained_reason, application_id),
        )

    def qualify(self, application: ApplicationRecord, now: str) -> None:
        amount = application["nominal_paise"]
        self.connection.execute(
            "UPDATE budgets SET held_paise=held_paise-%s,committed_paise=committed_paise+%s WHERE offer_id=%s",
            (amount, amount, application["offer_id"]),
        )
        self.connection.execute(
            "UPDATE customer_usage SET held_count=held_count-1,committed_count=committed_count+1 WHERE offer_id=%s AND user_id=%s",
            (application["offer_id"], application["user_id"]),
        )
        self.connection.execute(
            "UPDATE applications SET state='OWED',payment_state='CAPTURED',benefit_state='OWED',budget_state='COMMITTED',reason=NULL WHERE application_id=%s",
            (application["application_id"],),
        )
        self.connection.execute(
            "INSERT INTO benefit_obligations(application_id,offer_id,user_id,amount_paise,payout,state,owed_at,fulfilled_at) VALUES (%s,%s,%s,%s,%s,'OWED',%s,NULL)",
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
            "UPDATE applications SET state=%s,benefit_state='FULFILLED',reason=%s WHERE application_id=%s",
            (state, retained_reason, application_id),
        )
        self.connection.execute(
            "UPDATE benefit_obligations SET state='FULFILLED',fulfilled_at=%s WHERE application_id=%s",
            (now, application_id),
        )

    def advance_sequence(self, application_id: str, sequence: int) -> None:
        self.connection.execute(
            "UPDATE applications SET last_sequence=GREATEST(last_sequence,%s) WHERE application_id=%s",
            (sequence, application_id),
        )


class EventRepository:
    """Append financial journal facts alongside the mutation they describe."""

    def __init__(self, connection: Connection[Record]) -> None:
        self.connection = connection

    def append(self, application_id: str | None, kind: str, payload: Any, now: str) -> None:
        self.connection.execute(
            "INSERT INTO events(application_id,kind,payload_json,occurred_at) VALUES (%s,%s,%s,%s)",
            (application_id, kind, canonical(payload), now),
        )


class ReportRepository:
    """Read the consistent rows required by journal reconciliation."""

    def __init__(self, connection: Connection[Record]) -> None:
        self.connection = connection

    def snapshot(self) -> dict[str, list[Record]]:
        return {
            table: self.connection.execute(
                f"SELECT * FROM {table}" + (" ORDER BY event_sequence" if table == "events" else "")
            ).fetchall()
            for table in ("budgets", "events", "applications", "benefit_obligations")
        }


class WorkQueueRepository:
    """List bounded work; use cases recheck state in their own atomic claims."""

    def __init__(self, connection: Connection[Record]) -> None:
        self.connection = connection

    def ready(self, limit: int) -> list[str]:
        return [
            row["application_id"]
            for row in self.connection.execute(
                "SELECT a.application_id FROM applications a JOIN outbox o USING(application_id) WHERE a.state='HELD' AND a.budget_state='HELD' AND o.state='READY' ORDER BY a.application_id LIMIT %s",
                (limit,),
            )
        ]

    def unresolved(self, after: str, limit: int) -> list[str]:
        return [
            row["application_id"]
            for row in self.connection.execute(
                "SELECT application_id FROM applications WHERE application_id>%s AND (state IN ('DISPATCHING','PENDING','UNKNOWN') OR benefit_state='OWED') ORDER BY application_id LIMIT %s",
                (after, limit),
            )
        ]


class PostgreSQLUnitOfWork:
    """Attach every repository to the same locked PostgreSQL transaction."""

    def __init__(self, connection: Connection[Record]) -> None:
        self.snapshots = SnapshotRepository(connection)
        self.checkouts = CheckoutRepository(connection)
        self.admission = AdmissionRepository(connection)
        self.dispatch = DispatchRepository(connection)
        self.evidence = EvidenceRepository(connection)
        self.events = EventRepository(connection)
        self.reports = ReportRepository(connection)
        self.work = WorkQueueRepository(connection)
