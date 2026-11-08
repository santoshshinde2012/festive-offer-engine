from __future__ import annotations

import json
from dataclasses import dataclass

from .ports import Record, ReportUnits


@dataclass(frozen=True)
class ReportingService:
    """Rebuild financial totals from the journal without changing operational state."""

    units: ReportUnits

    def report(self) -> Record:
        with self.units.unit_of_work() as uow:
            snapshot = uow.reports.snapshot()
        budgets, events = snapshot["budgets"], snapshot["events"]
        applications, obligations = snapshot["applications"], snapshot["benefit_obligations"]
        replay: dict[str, int] = {}
        per_offer = {row["offer_id"]: {"held": 0, "committed": 0} for row in budgets}
        application_offers = {row["application_id"]: row["offer_id"] for row in applications}
        for event in events:
            kind = event["kind"]
            if kind not in {
                "liability_reserved",
                "liability_released",
                "payment_qualified_benefit_owed",
                "benefit_fulfilled",
            }:
                continue
            amount = json.loads(event["payload_json"])["amount_paise"]
            replay[kind] = replay.get(kind, 0) + amount
            totals = per_offer[application_offers[event["application_id"]]]
            if kind == "liability_reserved":
                totals["held"] += amount
            elif kind == "liability_released":
                totals["held"] -= amount
            elif kind == "payment_qualified_benefit_owed":
                totals["held"] -= amount
                totals["committed"] += amount
        return {
            "conservation": [
                {
                    **row,
                    "conserved": row["funding_paise"]
                    == row["available_paise"] + row["held_paise"] + row["committed_paise"],
                }
                for row in budgets
            ],
            "replay_totals_paise": replay,
            "replay_budget_reconciliation": [
                {
                    "offer_id": row["offer_id"],
                    "events_held_paise": per_offer[row["offer_id"]]["held"],
                    "events_committed_paise": per_offer[row["offer_id"]]["committed"],
                    "matches_native_state": per_offer[row["offer_id"]]["held"] == row["held_paise"]
                    and per_offer[row["offer_id"]]["committed"] == row["committed_paise"],
                }
                for row in budgets
            ],
            "reconciliation_required": [
                {
                    "application_id": row["application_id"],
                    "provider_reference": row["provider_reference"],
                    "state": row["state"],
                    "reason": row["reason"],
                    "nominal_paise": row["nominal_paise"],
                }
                for row in applications
                if row["state"] in {"UNKNOWN", "DISPATCHING"}
            ],
            "owed_not_fulfilled": [row for row in obligations if row["state"] == "OWED"],
        }
