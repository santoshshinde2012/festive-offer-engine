"""HTTP transport for the companion's normalized, explicitly synthetic contract.

This is an integration boundary, not a Stripe, bank or issuer implementation.
The remote service must honor the original reference as its idempotency key and
return signed normalized evidence. Business services authenticate that evidence.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any, NoReturn
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .models import PaymentBinding, ProviderMessage, canonical


class ProviderTransportError(RuntimeError):
    """Report uncertainty without exposing a token or a provider response body."""


class ProviderResponseError(ValueError):
    """Reject an incompatible normalized response before it reaches the ledger."""


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError("Nonfinite JSON number")


class _NoRedirect(HTTPRedirectHandler):
    """Keep authorization and payment commands on the configured origin."""

    def redirect_request(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


@dataclass(frozen=True)
class HttpProvider:
    """Use one bounded request per operation; ambiguous submission needs lookup.

    POST ``/payments/{reference}`` accepts the PaymentBinding JSON object and an
    ``Idempotency-Key`` equal to that reference. Its JSON acknowledgement must be
    ``{"provider_reference": reference, "accepted": true}``. GET on the same
    path plus ``/events`` returns ``[{"payload": {...}, "signature": "..."}]``.
    HTTPS is mandatory except for explicitly enabled loopback fixture servers.
    Redirects and implicit retries are deliberately disabled.
    """

    base_url: str
    bearer_token: str = field(repr=False)
    timeout_seconds: float = 5.0
    max_response_bytes: int = 1_048_576
    allow_insecure_loopback: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.base_url, str) or any(ord(char) < 33 for char in self.base_url):
            raise ValueError("Provider base URL must not contain whitespace or control characters")
        if type(self.allow_insecure_loopback) is not bool:
            raise ValueError("Loopback HTTP opt-in must be a boolean")
        parsed = urlsplit(self.base_url)
        # Accessing port also rejects malformed ports before a request is made.
        _ = parsed.port
        loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.scheme not in {"http", "https"}
            or (parsed.scheme == "http" and not (loopback and self.allow_insecure_loopback))
        ):
            raise ValueError(
                "Provider requires an HTTPS origin without URL credentials, query or fragment"
            )
        if (
            not isinstance(self.bearer_token, str)
            or not self.bearer_token
            or any(ord(char) < 33 or ord(char) > 126 for char in self.bearer_token)
        ):
            raise ValueError("Provider bearer token must be a nonempty visible ASCII token")
        if (
            type(self.timeout_seconds) not in {int, float}
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("Provider timeout must be finite and positive")
        if type(self.max_response_bytes) is not int or self.max_response_bytes < 1:
            raise ValueError("Provider response limit must be a positive integer")

    def _url(self, reference: str, suffix: str = "") -> str:
        if (
            not isinstance(reference, str)
            or not reference
            or len(reference) > 200
            or any(ord(char) < 33 for char in reference)
        ):
            raise ValueError("Provider reference must be a bounded nonempty identifier")
        return f"{self.base_url.rstrip('/')}/payments/{quote(reference, safe='')}{suffix}"

    def _request(
        self,
        method: str,
        url: str,
        body: dict[str, Any] | None = None,
        reference: str | None = None,
    ) -> Any:
        headers = {"Authorization": f"Bearer {self.bearer_token}", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if reference is not None:
            headers["Idempotency-Key"] = reference
        request = Request(
            url,
            data=canonical(body).encode() if body is not None else None,
            headers=headers,
            method=method,
        )
        try:
            with build_opener(_NoRedirect()).open(
                request, timeout=self.timeout_seconds
            ) as response:
                if response.headers.get_content_type() != "application/json":
                    raise ProviderResponseError("Provider response must be JSON")
                raw = response.read(self.max_response_bytes + 1)
        except HTTPError as exc:
            # Even a 4xx may follow an accepted effect in a broken intermediary.
            # No caller should infer a definitive payment failure from transport.
            exc.close()
            raise ProviderTransportError(
                f"Provider HTTP status {exc.code}; reconcile original reference"
            ) from None
        except (URLError, TimeoutError, OSError):
            raise ProviderTransportError(
                "Provider request did not complete; reconcile original reference"
            ) from None
        if len(raw) > self.max_response_bytes:
            raise ProviderResponseError("Provider response exceeded configured byte limit")
        try:
            return json.loads(raw, parse_constant=_reject_json_constant)
        except (UnicodeDecodeError, ValueError):
            raise ProviderResponseError("Provider returned invalid JSON") from None

    def submit(self, reference: str, binding: PaymentBinding) -> None:
        """Send the original reference once; never retry after ambiguous transport."""
        result = self._request("POST", self._url(reference), asdict(binding), reference)
        if (
            not isinstance(result, dict)
            or result.get("provider_reference") != reference
            or result.get("accepted") is not True
        ):
            raise ProviderResponseError(
                "Provider acknowledgement did not confirm the original reference"
            )

    def lookup(self, reference: str) -> tuple[ProviderMessage, ...]:
        """Return complete, sequence-ordered evidence for the original reference."""
        result = self._request("GET", self._url(reference, "/events"))
        if not isinstance(result, list):
            raise ProviderResponseError("Provider event response must be a list")
        messages: list[ProviderMessage] = []
        sequences: set[int] = set()
        identities: set[str] = set()
        for item in result:
            if (
                not isinstance(item, dict)
                or set(item) != {"payload", "signature"}
                or not isinstance(item["payload"], dict)
                or not isinstance(item["signature"], str)
                or re.fullmatch(r"[0-9a-fA-F]{64}", item["signature"]) is None
            ):
                raise ProviderResponseError("Provider event envelope is invalid")
            payload = item["payload"]
            sequence, identity = payload.get("sequence"), payload.get("event_id")
            if (
                payload.get("provider_reference") != reference
                or type(sequence) is not int
                or sequence < 1
                or not isinstance(identity, str)
                or not identity
                or sequence in sequences
                or identity in identities
            ):
                raise ProviderResponseError(
                    "Provider event reference, identity or sequence is invalid"
                )
            sequences.add(sequence)
            identities.add(identity)
            messages.append(ProviderMessage(payload, item["signature"]))
        return tuple(sorted(messages, key=lambda message: message.payload["sequence"]))
