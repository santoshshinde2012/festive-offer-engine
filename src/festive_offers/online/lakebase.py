from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import Lock
from typing import Any


@dataclass(frozen=True)
class LakebaseSettings:
    """Require an explicit Lakebase endpoint without storing credentials in configuration."""

    endpoint: str
    host: str
    database: str
    user: str
    port: int = 5432
    sslmode: str = "require"
    connect_timeout: int = 10
    sslrootcert: str | None = None

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> LakebaseSettings:
        """Read connection coordinates; Databricks unified auth remains SDK-owned."""
        required = ("ENDPOINT_NAME", "PGHOST", "PGDATABASE", "PGUSER")
        if any(not environ.get(name, "").strip() for name in required):
            raise ValueError("Lakebase requires ENDPOINT_NAME, PGHOST, PGDATABASE and PGUSER")
        endpoint = environ["ENDPOINT_NAME"].strip()
        if not re.fullmatch(r"projects/[^/\s]+/branches/[^/\s]+/endpoints/[^/\s]+", endpoint):
            raise ValueError("ENDPOINT_NAME must be projects/<id>/branches/<id>/endpoints/<id>")
        sslmode = environ.get("PGSSLMODE", "require")
        if sslmode not in {"require", "verify-ca", "verify-full"}:
            raise ValueError(
                "Lakebase PGSSLMODE must require TLS: require, verify-ca or verify-full"
            )
        try:
            port = int(environ.get("PGPORT", "5432"))
            timeout = int(environ.get("PGCONNECT_TIMEOUT", "10"))
        except ValueError as exc:
            raise ValueError("PGPORT and PGCONNECT_TIMEOUT must be integers") from exc
        if not 1 <= port <= 65535 or not 1 <= timeout <= 300:
            raise ValueError("PGPORT must be 1..65535 and PGCONNECT_TIMEOUT must be 1..300 seconds")
        return cls(
            endpoint=endpoint,
            host=environ["PGHOST"].strip(),
            database=environ["PGDATABASE"].strip(),
            user=environ["PGUSER"].strip(),
            port=port,
            sslmode=sslmode,
            connect_timeout=timeout,
            sslrootcert=environ.get("PGSSLROOTCERT") or None,
        )


def make_connection_factory(
    *,
    environ: Mapping[str, str] | None = None,
    workspace_client: Any | None = None,
    connect: Callable[..., Any] | None = None,
) -> Callable[[], Any]:
    """Open fresh psycopg connections with fresh Lakebase Autoscaling OAuth credentials.

    The returned callable performs no SQL. The PostgreSQL store owns transaction
    setup and connection closure. Injected client/connector arguments let tests
    check credential rotation without pretending to exercise a cloud endpoint.
    """
    settings = LakebaseSettings.from_environment(os.environ if environ is None else environ)
    client = workspace_client
    connector = connect
    initialization_lock = Lock()

    def open_connection() -> Any:
        nonlocal client, connector
        with initialization_lock:
            if client is None:
                try:
                    from databricks.sdk import WorkspaceClient
                except ImportError as exc:
                    raise RuntimeError(
                        "Install requirements-databricks.txt to connect to Lakebase"
                    ) from exc
                client = WorkspaceClient()
            if connector is None:
                try:
                    import psycopg
                except ImportError as exc:
                    raise RuntimeError(
                        "Install requirements-databricks.txt to connect to Lakebase"
                    ) from exc
                connector = psycopg.connect

        # Credentials expire. Never capture one token when the factory is built,
        # and never fall back to an environment password if credential minting fails.
        credential = client.postgres.generate_database_credential(endpoint=settings.endpoint)
        token = credential.token
        if not isinstance(token, str) or not token:
            raise RuntimeError("Lakebase did not return a database credential")
        kwargs: dict[str, Any] = {
            "host": settings.host,
            "dbname": settings.database,
            "user": settings.user,
            "port": settings.port,
            "sslmode": settings.sslmode,
            "connect_timeout": settings.connect_timeout,
            "application_name": "festive-offers",
            "password": token,
        }
        if settings.sslrootcert:
            kwargs["sslrootcert"] = settings.sslrootcert
        return connector(**kwargs)

    return open_connection
