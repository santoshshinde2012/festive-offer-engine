"""Validate runtime configuration before opening databases or provider resources."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, cast

from .http_provider import HttpProvider
from .lakebase import LakebaseSettings
from .postgres_store import validate_schema_name

ProviderKind = Literal["fake", "http"]
StoreKind = Literal["sqlite", "postgres", "lakebase"]


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if not value:
        raise ValueError(f"{name} is required for the selected adapter")
    return value


@dataclass(frozen=True)
class RuntimeSettings:
    """Keep a validated adapter selection and secret-safe connection coordinates."""

    provider_kind: ProviderKind
    store_kind: StoreKind
    sqlite_path: Path
    provider_database_path: Path
    synthetic_now: datetime | None
    provider_url: str = ""
    provider_token: str = field(default="", repr=False)
    provider_timeout_seconds: float = 5.0
    event_key: bytes = field(default=b"", repr=False)
    postgres_dsn: str = field(default="", repr=False)
    postgres_schema: str = "festive_offers"
    initialize_postgres_schema: bool = True

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> RuntimeSettings:
        provider_name = environ.get("FESTIVE_PROVIDER", "fake")
        store_name = environ.get("FESTIVE_STORE", "sqlite")
        if provider_name not in {"fake", "http"}:
            raise ValueError("FESTIVE_PROVIDER must be fake or http")
        if store_name not in {"sqlite", "postgres", "lakebase"}:
            raise ValueError("FESTIVE_STORE must be sqlite, postgres or lakebase")
        frozen_time = environ.get("FESTIVE_SYNTHETIC_NOW")
        if frozen_time and provider_name != "fake":
            raise ValueError("A frozen synthetic clock is allowed only with the fake provider")
        synthetic_now = (
            datetime.fromisoformat(frozen_time.replace("Z", "+00:00")) if frozen_time else None
        )
        if synthetic_now is not None and synthetic_now.tzinfo is None:
            raise ValueError("FESTIVE_SYNTHETIC_NOW must have a timezone")

        provider_url = provider_token = ""
        timeout = 5.0
        event_key = b""
        if provider_name == "http":
            provider_url = _required(environ, "FESTIVE_PROVIDER_URL")
            provider_token = _required(environ, "FESTIVE_PROVIDER_TOKEN")
            timeout = float(environ.get("FESTIVE_PROVIDER_TIMEOUT", "5"))
            if not math.isfinite(timeout) or timeout <= 0:
                raise ValueError("FESTIVE_PROVIDER_TIMEOUT must be finite and positive")
            event_key = _required(environ, "FESTIVE_EVENT_KEY").encode()
            if len(event_key) < 32:
                raise ValueError("FESTIVE_EVENT_KEY must be at least 32 bytes")
            # This immutable transport validates its origin without sending a request.
            HttpProvider(provider_url, provider_token, timeout_seconds=timeout)

        schema = environ.get("FESTIVE_POSTGRES_SCHEMA", "festive_offers")
        initialize = environ.get("FESTIVE_POSTGRES_INITIALIZE", "true")
        postgres_dsn = ""
        if store_name in {"postgres", "lakebase"}:
            validate_schema_name(schema)
            if initialize not in {"true", "false"}:
                raise ValueError("FESTIVE_POSTGRES_INITIALIZE must be true or false")
            if store_name == "postgres":
                postgres_dsn = _required(environ, "FESTIVE_POSTGRES_DSN")
            else:
                LakebaseSettings.from_environment(environ)
        return cls(
            provider_kind=cast(ProviderKind, provider_name),
            store_kind=cast(StoreKind, store_name),
            sqlite_path=Path(
                environ.get("FESTIVE_SQLITE_PATH", "output/runtime/application.sqlite")
            ),
            provider_database_path=Path(
                environ.get("FESTIVE_PROVIDER_DB", "output/runtime/provider.sqlite")
            ),
            synthetic_now=synthetic_now,
            provider_url=provider_url,
            provider_token=provider_token,
            provider_timeout_seconds=timeout,
            event_key=event_key,
            postgres_dsn=postgres_dsn,
            postgres_schema=schema,
            initialize_postgres_schema=initialize == "true",
        )
