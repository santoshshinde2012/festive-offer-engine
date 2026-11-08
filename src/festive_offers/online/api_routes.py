"""Small role-specific endpoint groups; parsing stays outside financial use cases."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol

from ..serialization import dataset_from_dict
from .api_access import CampaignActions, CustomerOfferAccess, EvidenceActions, MerchantInputActions
from .api_models import ApiError, ApiResponse, HeaderReader, Principal, object_fields, required_text
from .authentication import require_role
from .models import ProviderMessage


class EndpointGroup(Protocol):
    """Return None only when another endpoint group should match the request."""

    def handle(
        self, method: str, path: str, body: Any, headers: HeaderReader, principal: Principal
    ) -> ApiResponse | None: ...


def _customer_id(principal: Principal) -> str:
    require_role(principal, "customer")
    if not principal.user_id:
        raise ApiError(403, "Customer identity required")
    return principal.user_id


@dataclass(frozen=True)
class CustomerEndpoints:
    """Accept references and choices while ownership stays in the customer use case."""

    access: CustomerOfferAccess

    def handle(
        self, method: str, path: str, body: Any, headers: HeaderReader, principal: Principal
    ) -> ApiResponse | None:
        if method == "POST" and path == "/v1/quotes":
            user_id = _customer_id(principal)
            data = object_fields(body, {"checkout_id"})
            return 201, asdict(
                self.access.quote_for_customer(user_id, required_text(data["checkout_id"]))
            )
        if method == "POST" and path == "/v1/applications":
            user_id = _customer_id(principal)
            data = object_fields(body, {"quote_id", "offer_id"})
            application = self.access.apply_for_customer(
                user_id,
                required_text(data["quote_id"]),
                required_text(data["offer_id"]),
                required_text(headers.get("Idempotency-Key")),
            )
            return 200, asdict(application)
        prefix = "/v1/applications/"
        if method == "GET" and path.startswith(prefix):
            user_id = _customer_id(principal)
            application = self.access.application_for_customer(
                user_id, required_text(path[len(prefix) :])
            )
            return 200, asdict(application)
        return None


@dataclass(frozen=True)
class MerchantEndpoints:
    """Keep trusted merchant facts unavailable to customer credentials."""

    actions: MerchantInputActions

    def handle(
        self, method: str, path: str, body: Any, headers: HeaderReader, principal: Principal
    ) -> ApiResponse | None:
        if method == "POST" and path == "/v1/merchant/checkouts":
            require_role(principal, "merchant")
            data = object_fields(body, {"checkout", "payment_id"})
            object_fields(
                data["checkout"], {"request_id", "user_id", "merchant", "at", "items", "currency"}
            )
            checkout = dataset_from_dict(
                {"offers": [], "wallets": [], "checkouts": [data["checkout"]], "assumptions": {}}
            ).checkouts[0]
            self.actions.update_checkout(checkout, required_text(data["payment_id"]))
            return 200, {"checkout_id": checkout.request_id}
        if method == "POST" and path == "/v1/merchant/wallets":
            require_role(principal, "merchant")
            data = object_fields(body, {"wallets"})
            self.actions.seed_wallets(data["wallets"])
            return 200, {"accepted": True}
        return None


@dataclass(frozen=True)
class OperatorEndpoints:
    """Expose publication, campaign switches and reporting as operator capabilities."""

    actions: CampaignActions

    def handle(
        self, method: str, path: str, body: Any, headers: HeaderReader, principal: Principal
    ) -> ApiResponse | None:
        if method == "POST" and path == "/v1/operator/publications":
            require_role(principal, "operator")
            data = object_fields(body, {"bundle", "funding_paise"}, {"customer_limit"})
            revision = self.actions.prepare_snapshot(
                data["bundle"], data["funding_paise"], data.get("customer_limit", 1)
            )
            return 201, {"revision": revision, "state": "PREPARED"}
        prefix = "/v1/operator/publications/"
        if method == "POST" and path.startswith(prefix) and path.endswith("/activate"):
            require_role(principal, "operator")
            object_fields(body, set())
            revision = required_text(path[len(prefix) : -len("/activate")])
            self.actions.activate_snapshot(revision)
            return 200, {"revision": revision, "state": "ACTIVE"}
        prefix = "/v1/operator/campaigns/"
        if method == "POST" and path.startswith(prefix):
            require_role(principal, "operator")
            data = object_fields(body, {"enabled"})
            if type(data["enabled"]) is not bool:
                raise ApiError(400, "enabled must be a boolean")
            self.actions.set_campaign_enabled(required_text(path[len(prefix) :]), data["enabled"])
            return 200, {"enabled": data["enabled"]}
        if method == "GET" and path == "/v1/operator/report":
            require_role(principal, "operator")
            return 200, self.actions.report()
        return None


@dataclass(frozen=True)
class ProviderEndpoints:
    """Authenticate transport role separately from signed payment evidence."""

    actions: EvidenceActions

    def handle(
        self, method: str, path: str, body: Any, headers: HeaderReader, principal: Principal
    ) -> ApiResponse | None:
        if method != "POST" or path != "/v1/provider/events":
            return None
        require_role(principal, "provider")
        data = object_fields(body, {"payload", "signature"})
        if not isinstance(data["payload"], dict):
            raise ApiError(400, "payload must be an object")
        disposition = self.actions.receive_event(
            ProviderMessage(data["payload"], required_text(data["signature"]))
        )
        return 200, {"disposition": disposition}
