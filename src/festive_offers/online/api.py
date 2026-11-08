"""HTTP framing and a compatibility facade for explicit authenticated endpoints."""

from __future__ import annotations

import json
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .api_access import (
    ApiActions,
    CustomerAccessUnits,
    CustomerOfferAccess,
    OwnedResourceNotFoundError,
)
from .api_models import (
    ApiError,
    ApiResponse,
    HeaderReader,
    Principal,
    RequestDispatcher,
    object_fields,
    required_text,
)
from .api_routes import (
    CustomerEndpoints,
    EndpointGroup,
    MerchantEndpoints,
    OperatorEndpoints,
    ProviderEndpoints,
)
from .authentication import Authenticator, TokenAuthenticator, require_role
from .models import canonical
from .ports import Record

__all__ = ["ApiError", "OfferApi", "Principal", "make_server", "object_fields", "text"]

# Public names retained for existing local launchers and the browser fixture.
text = required_text


class OfferApi:
    """Compose role endpoints without owning their authentication or business logic."""

    def __init__(
        self,
        service: ApiActions,
        units: CustomerAccessUnits,
        tokens: Mapping[str, Principal] | None = None,
        *,
        authenticator: Authenticator | None = None,
    ) -> None:
        if authenticator is not None and tokens is not None:
            raise ValueError("Provide configured tokens or an authenticator, not both")
        self.authenticator = (
            authenticator if authenticator is not None else TokenAuthenticator(tokens or {})
        )
        self.service, self.units = service, units
        self._endpoints: tuple[EndpointGroup, ...] = (
            CustomerEndpoints(CustomerOfferAccess(service, units)),
            MerchantEndpoints(service),
            OperatorEndpoints(service),
            ProviderEndpoints(service),
        )

    def authenticate(self, authorization: str | None) -> Principal:
        """Preserve the fixture extension point while delegating authentication."""
        return self.authenticator.authenticate(authorization)

    require = staticmethod(require_role)

    def handle(self, method: str, path: str, body: Any, headers: HeaderReader) -> ApiResponse:
        if method == "GET" and path == "/health":
            return 200, {"status": "ok", "scope": "reference application"}
        principal = self.authenticate(headers.get("Authorization"))
        for endpoints in self._endpoints:
            try:
                response = endpoints.handle(method, path, body, headers, principal)
            except OwnedResourceNotFoundError as error:
                raise ApiError(404, str(error)) from None
            if response is not None:
                return response
        raise ApiError(404, "Endpoint not found")


def _unique_object(pairs: list[tuple[str, Any]]) -> Record:
    result: Record = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def make_server(
    api: RequestDispatcher, host: str = "127.0.0.1", port: int = 8080
) -> ThreadingHTTPServer:
    """Create a loopback reference server; deploy behind a supported TLS/auth gateway."""

    web = Path(__file__).resolve().parents[3] / "web"
    assets = {
        "/": ("index.html", "text/html; charset=utf-8"),
        "/app.js": ("app.js", "text/javascript; charset=utf-8"),
        "/style.css": ("style.css", "text/css; charset=utf-8"),
    }

    class Handler(BaseHTTPRequestHandler):
        server_version = "FestiveReference/1"
        MAX_BODY = 262144

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, format: str, *args: Any) -> None:
            # Never log bearer headers, customer inputs or provider payloads.
            return

        def do_GET(self) -> None:
            self.respond()

        def do_POST(self) -> None:
            self.respond()

        def respond(self) -> None:
            try:
                parsed = urlsplit(self.path)
                if parsed.query or parsed.fragment or parsed.scheme or parsed.netloc:
                    raise ApiError(400, "Use an endpoint path without query parameters")
                if self.command == "GET" and parsed.path in assets:
                    filename, content_type = assets[parsed.path]
                    encoded = (web / filename).read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(encoded)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header(
                        "Content-Security-Policy",
                        "default-src 'self'; base-uri 'none'; frame-ancestors 'none'",
                    )
                    self.end_headers()
                    self.wfile.write(encoded)
                    return
                body: Any = None
                if self.command == "POST":
                    # Reject ambiguous framing before attempting any mutation.
                    lengths = self.headers.get_all("Content-Length", [])
                    if self.headers.get("Transfer-Encoding") or len(lengths) != 1:
                        raise ApiError(400, "One Content-Length header is required")
                    try:
                        length = int(lengths[0])
                    except ValueError as exc:
                        raise ApiError(400, "Invalid Content-Length") from exc
                    if not 0 < length <= self.MAX_BODY:
                        raise ApiError(413, "JSON body exceeds the reference limit")
                    if self.headers.get_content_type() != "application/json":
                        raise ApiError(415, "Use application/json")
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise ApiError(400, "Incomplete JSON body")

                    def invalid_constant(value: str) -> None:
                        raise ValueError("Nonfinite JSON number")

                    body = json.loads(
                        raw, object_pairs_hook=_unique_object, parse_constant=invalid_constant
                    )
                if (
                    len(self.headers.get_all("Authorization", [])) > 1
                    or len(self.headers.get_all("Idempotency-Key", [])) > 1
                ):
                    raise ApiError(400, "Duplicate authentication or idempotency header")
                status, result = api.handle(self.command, parsed.path, body, self.headers)
            except ApiError as exc:
                status, result = exc.status, {"error": str(exc)}
            except (ValueError, TypeError, KeyError, AttributeError, UnicodeError) as exc:
                status, result = (
                    400,
                    {
                        "error": "Invalid input or action conflicts with current state",
                        "kind": type(exc).__name__,
                    },
                )
            except Exception:
                status, result = (
                    500,
                    {"error": "The operation could not be completed; retry with the same identity"},
                )
            encoded = (canonical(result) + "\n").encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(encoded)
            self.close_connection = True

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server
