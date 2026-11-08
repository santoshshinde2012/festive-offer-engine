"""Reference bearer authentication independent of routing and business operations."""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from typing import Protocol

from .api_models import ApiError, Principal, Role


class Authenticator(Protocol):
    """Resolve a trusted configured caller without accepting identity in a body."""

    def authenticate(self, authorization: str | None) -> Principal: ...


class TokenAuthenticator:
    """Validate configured identities up front and compare credentials without logging."""

    def __init__(self, tokens: Mapping[str, Principal]) -> None:
        if not tokens or any(
            not isinstance(token, str)
            or not 24 <= len(token) <= 4096
            or any(ord(character) < 33 or ord(character) > 126 for character in token)
            for token in tokens
        ):
            raise ValueError(
                "Configure distinct bearer tokens of 24..4096 visible ASCII characters"
            )
        for principal in tokens.values():
            if (
                not isinstance(principal, Principal)
                or not isinstance(principal.role, str)
                or principal.role not in {"customer", "merchant", "operator", "provider"}
                or (
                    principal.role == "customer"
                    and (not isinstance(principal.user_id, str) or not principal.user_id)
                )
            ):
                raise ValueError("Invalid token role or missing customer identity")
        self._tokens = dict(tokens)

    def authenticate(self, authorization: str | None) -> Principal:
        """Return the configured identity; never echo a supplied or configured token."""
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            raise ApiError(401, "Bearer authentication required")
        supplied = authorization[7:]
        if not supplied.isascii():
            raise ApiError(401, "Invalid credential")
        for token, principal in self._tokens.items():
            if hmac.compare_digest(token, supplied):
                return principal
        raise ApiError(401, "Invalid credential")


def require_role(principal: Principal, role: Role) -> None:
    """Keep role authorization explicit at the matching endpoint boundary."""
    if principal.role != role:
        raise ApiError(403, "This credential cannot perform this action")
