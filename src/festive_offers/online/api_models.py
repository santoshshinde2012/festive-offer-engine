"""Small HTTP boundary values shared by authentication, routes and the server."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from .ports import Record

Role = Literal["customer", "merchant", "operator", "provider"]
ApiResponse = tuple[int, Record]


@dataclass(frozen=True)
class Principal:
    """Bind a configured caller to one role and an optional customer identity."""

    role: Role
    user_id: str | None = None


class ApiError(Exception):
    """Carry an intentional public HTTP result without exposing adapter errors."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class HeaderReader(Protocol):
    """Read only the headers needed by routes, without requiring a mutable mapping."""

    def get(self, name: str, /) -> str | None: ...


class RequestDispatcher(Protocol):
    """Let the HTTP server serve a request handler without a concrete application."""

    def handle(self, method: str, path: str, body: Any, headers: HeaderReader) -> ApiResponse: ...


def object_fields(value: Any, required: set[str], optional: set[str] | None = None) -> Record:
    """Reject missing or extra JSON fields rather than trust ambiguous client facts."""
    if (
        not isinstance(value, dict)
        or not required <= value.keys()
        or value.keys() - required - (optional or set())
    ):
        raise ApiError(400, "Missing or unsupported JSON fields")
    return value


def required_text(value: Any) -> str:
    """Require a bounded identifier before invoking an application operation."""
    if not isinstance(value, str) or not 1 <= len(value) <= 200:
        raise ApiError(400, "Expected a nonempty string of at most 200 characters")
    return value
