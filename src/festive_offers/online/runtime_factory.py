"""Compose concrete adapters at the runtime boundary, outside financial use cases."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..ranking import ExpectedSavingsScorer
from .api_models import Principal
from .authentication import TokenAuthenticator
from .fake_provider import SYNTHETIC_EVENT_KEY, FakeProvider
from .http_provider import HttpProvider
from .models import Clock, FrozenClock, SystemClock
from .offer_service import OfferService
from .operational_export import ExportManifest
from .ports import Provider, UnitOfWorkFactory
from .runtime_settings import RuntimeSettings
from .security import HmacEventVerifier
from .sqlite_store import SQLiteStore


class RuntimeStore(UnitOfWorkFactory, Protocol):
    """Add delayed analytical export to the operational transaction boundary."""

    def export(self, destination: Path, reports: dict[str, Any]) -> ExportManifest: ...


@dataclass(frozen=True)
class Runtime:
    """Hold explicit runtime dependencies without hidden deployment side effects."""

    store: RuntimeStore
    service: OfferService
    simulated_provider: FakeProvider | None


def build_runtime(environ: Mapping[str, str] | None = None) -> Runtime:
    """Validate a stable environment snapshot before creating concrete resources."""
    env = dict(os.environ if environ is None else environ)
    settings = RuntimeSettings.from_environment(env)
    simulated: FakeProvider | None = None
    provider: Provider
    if settings.provider_kind == "fake":
        simulated = FakeProvider(settings.provider_database_path)
        provider = simulated
        event_key = SYNTHETIC_EVENT_KEY
    else:
        provider = HttpProvider(
            settings.provider_url,
            settings.provider_token,
            timeout_seconds=settings.provider_timeout_seconds,
        )
        event_key = settings.event_key
    clock: Clock = (
        FrozenClock(settings.synthetic_now) if settings.synthetic_now is not None else SystemClock()
    )
    store = _build_store(settings, env)
    service = OfferService(
        store,
        provider,
        clock,
        verifier=HmacEventVerifier(event_key),
        scorer=ExpectedSavingsScorer(),
    )
    return Runtime(store, service, simulated)


def _build_store(settings: RuntimeSettings, environ: Mapping[str, str]) -> RuntimeStore:
    if settings.store_kind == "sqlite":
        return SQLiteStore(settings.sqlite_path)
    from .postgres_store import PostgreSQLStore

    if settings.store_kind == "postgres":
        return PostgreSQLStore(
            settings.postgres_dsn,
            schema=settings.postgres_schema,
            initialize=settings.initialize_postgres_schema,
        )
    from .lakebase import make_connection_factory

    return PostgreSQLStore(
        connection_factory=make_connection_factory(environ=environ),
        schema=settings.postgres_schema,
        initialize=settings.initialize_postgres_schema,
    )


def _unique_token_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate token or credential field")
        result[key] = value
    return result


def configured_tokens(environ: Mapping[str, str] | None = None) -> dict[str, Principal]:
    """Validate configured caller identities before starting the HTTP runtime."""
    env = os.environ if environ is None else environ
    encoded = env.get("FESTIVE_AUTH_TOKENS_JSON")
    if not encoded:
        raise ValueError("FESTIVE_AUTH_TOKENS_JSON is required for the HTTP server")
    raw = json.loads(encoded, object_pairs_hook=_unique_token_fields)
    if not isinstance(raw, dict):
        raise ValueError("FESTIVE_AUTH_TOKENS_JSON must map tokens to identities")
    tokens: dict[str, Principal] = {}
    for token, row in raw.items():
        if not isinstance(row, dict) or set(row) - {"role", "user_id"} or "role" not in row:
            raise ValueError("Unsupported or missing credential fields")
        tokens[token] = Principal(**row)
    TokenAuthenticator(tokens)
    return tokens
