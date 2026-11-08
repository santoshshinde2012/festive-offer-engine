from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from .models import Application, Clock, PaymentBinding, utc
from .policy import application_value
from .ports import ApplicationRecord, Provider, UnitOfWork, UnitOfWorkFactory


def release_liability(
    uow: UnitOfWork, application: ApplicationRecord, reason: str, now: str
) -> None:
    """Keep release mutation and its financial journal entry in the caller's unit."""
    if application["budget_state"] != "HELD":
        raise ValueError("Only an uncommitted held liability may be released")
    uow.dispatch.release(application, reason)
    uow.events.append(
        application["application_id"],
        "liability_released",
        {
            "amount_paise": application["nominal_paise"],
            "reason": reason,
        },
        now,
    )


@dataclass(frozen=True)
class PaymentService:
    """Claim outbox work before external submission and preserve ambiguous holds."""

    units: UnitOfWorkFactory
    provider: Provider
    clock: Clock

    def inspect(self, application_id: str) -> Application:
        with self.units.unit_of_work() as uow:
            application = uow.dispatch.get(application_id)
            if application is None:
                raise ValueError("Unknown application")
            return application_value(application)

    def expire_holds(self) -> int:
        with self.units.unit_of_work() as uow:
            now = utc(self.clock.now())
            applications = uow.dispatch.expired_ready(now)
            for application in applications:
                release_liability(uow, application, "expired_before_dispatch", now)
        return len(applications)

    def dispatch(self, application_id: str) -> Application:
        expired = False
        with self.units.unit_of_work() as uow:
            now = self.clock.now()
            application = uow.dispatch.get(application_id)
            if application is None:
                raise ValueError("Unknown application")
            if application["state"] != "HELD":
                return application_value(application)
            if now >= datetime.fromisoformat(application["expires_at"]):
                release_liability(uow, application, "expired_before_dispatch", utc(now))
                expired = True
            else:
                if not uow.dispatch.claim(application_id, utc(now)):
                    raise ValueError("Outbox command is not available to claim")
                uow.events.append(
                    application_id,
                    "dispatch_claimed",
                    {"provider_reference": application["provider_reference"]},
                    utc(now),
                )
                binding = PaymentBinding(**json.loads(application["binding_json"]))
                reference = application["provider_reference"]
        if expired:
            return self.inspect(application_id)
        # The repository transaction has ended before the external call begins.
        reason: str | None
        try:
            self.provider.submit(reference, binding)
        except Exception as exc:
            state, payment_state, outbox_state, reason = (
                "UNKNOWN",
                "UNKNOWN",
                "UNKNOWN",
                type(exc).__name__,
            )
        else:
            state, payment_state, outbox_state, reason = "PENDING", "PENDING", "SENT", None
        with self.units.unit_of_work() as uow:
            uow.dispatch.finish(application_id, state, payment_state, outbox_state, reason)
            uow.events.append(
                application_id,
                "dispatch_result",
                {"state": state, "reason": reason},
                utc(self.clock.now()),
            )
        return self.inspect(application_id)
