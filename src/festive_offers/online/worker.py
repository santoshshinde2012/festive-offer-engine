"""Bounded dispatch and original-reference recovery for a separately run worker."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol

from .models import Application
from .ports import WorkQueuePort


class RecoveryActions(Protocol):
    """Expose only the three operations needed by one recovery cycle."""

    def expire_holds(self) -> int: ...
    def dispatch(self, application_id: str) -> Application: ...
    def reconcile(self, application_id: str) -> str: ...


class WorkQueueUnit(Protocol):
    """Read bounded work without acquiring unrelated financial mutation methods."""

    @property
    def work(self) -> WorkQueuePort: ...


class WorkQueueUnits(Protocol):
    """Obtain one consistent work list through the smallest required unit contract."""

    def unit_of_work(self) -> AbstractContextManager[WorkQueueUnit]: ...


@dataclass(frozen=True)
class WorkResult:
    """Report attempts separately from financial facts received from a provider."""

    expired: int
    dispatch_attempts: int
    reconciliation_attempts: int
    errors: tuple[dict[str, str], ...]


class Worker:
    """Drain READY work and revisit uncertainty without issuing replacement payments."""

    def __init__(
        self, service: RecoveryActions, units: WorkQueueUnits, *, batch_size: int = 100
    ) -> None:
        if type(batch_size) is not int or not 1 <= batch_size <= 1000:
            raise ValueError("Worker batch_size must be between 1 and 1000")
        self.service = service
        self.units = units
        self.batch_size = batch_size
        self._reconciliation_cursor = ""

    def run_once(self) -> WorkResult:
        expired = self.service.expire_holds()
        with self.units.unit_of_work() as uow:
            ready = uow.work.ready(self.batch_size)
        errors: list[dict[str, str]] = []
        for application_id in ready:
            try:
                self.service.dispatch(application_id)
            except Exception as exc:
                errors.append(
                    {
                        "application_id": application_id,
                        "operation": "dispatch",
                        "error": type(exc).__name__,
                    }
                )
        with self.units.unit_of_work() as uow:
            pending = uow.work.unresolved(self._reconciliation_cursor, self.batch_size)
            if not pending and self._reconciliation_cursor:
                pending = uow.work.unresolved("", self.batch_size)
        self._reconciliation_cursor = pending[-1] if pending else ""
        for application_id in pending:
            try:
                self.service.reconcile(application_id)
            except Exception as exc:
                # A transport error is no evidence of failure: keep liability held.
                errors.append(
                    {
                        "application_id": application_id,
                        "operation": "reconcile",
                        "error": type(exc).__name__,
                    }
                )
        return WorkResult(expired, len(ready), len(pending), tuple(errors))
