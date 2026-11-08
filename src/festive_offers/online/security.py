"""Authentication adapter for the explicitly synthetic event contract."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any

from .models import ProviderMessage, canonical


def sign(payload: dict[str, Any], key: bytes) -> str:
    """Encode the synthetic transport signature without provider persistence."""
    return hmac.new(key, canonical(payload).encode(), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class HmacEventVerifier:
    """Check the local test signature without coupling use cases to HMAC."""

    key: bytes

    def verify(self, message: ProviderMessage) -> None:
        if not hmac.compare_digest(sign(message.payload, self.key), message.signature):
            raise ValueError("Unauthenticated synthetic provider message")
