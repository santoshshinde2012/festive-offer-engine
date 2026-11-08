"""Compatibility imports for the explicitly synthetic provider adapter."""

from __future__ import annotations

from .fake_provider import SYNTHETIC_EVENT_KEY, FakeProvider
from .security import sign

__all__ = ["FakeProvider", "SYNTHETIC_EVENT_KEY", "sign"]
