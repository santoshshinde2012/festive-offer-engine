from __future__ import annotations

from .models import Checkout, Dataset

MAX_REQUESTS = 100
MAX_OFFERS = 100
MAX_INSTRUMENTS = 20
MAX_BASKET_LINES = 100


def validate_run_id(run_id: str) -> None:
    """Reject unsafe run-directory identities before executing a batch."""
    if not run_id or not all(character.isalnum() or character in "_-" for character in run_id):
        raise ValueError("run_id must contain only letters, digits, underscore or hyphen")


def validate_checkout(request: Checkout) -> None:
    """Apply the same basket invariants to batch and operational input boundaries."""
    for field in ("request_id", "user_id", "merchant", "currency"):
        value = getattr(request, field)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field} must be a nonempty string")
    for item in request.items:
        for field in ("category", "brand"):
            value = getattr(item, field)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{field} must be a nonempty string")
    if len(request.items) > MAX_BASKET_LINES:
        raise ValueError("Demo limited to 100 basket lines per request")
    if (
        not request.items
        or any(
            type(item.net_amount_paise) is not int or item.net_amount_paise < 0
            for item in request.items
        )
        or request.at.tzinfo is None
    ):
        raise ValueError(
            "Requests need items, nonnegative integer net paise and timezone-aware timestamps"
        )


def validate_dataset(dataset: Dataset) -> None:
    """Check identities and bounded inputs without rejecting incomplete offer terms."""
    if (
        len(dataset.checkouts) > MAX_REQUESTS
        or len(dataset.offers) > MAX_OFFERS
        or len(dataset.wallets) > MAX_REQUESTS
    ):
        raise ValueError("Demo limited to 100 requests, offers and wallet rows")
    if any(len(request.items) > MAX_BASKET_LINES for request in dataset.checkouts):
        raise ValueError("Demo limited to 100 basket lines per request")
    if any(len(wallet) > MAX_INSTRUMENTS for wallet in dataset.wallets.values()):
        raise ValueError("Demo limited to 20 instruments per wallet")
    for label, identities in (
        ("offer", [offer.offer_id for offer in dataset.offers]),
        ("request", [request.request_id for request in dataset.checkouts]),
    ):
        if len(identities) != len(set(identities)):
            raise ValueError(f"Duplicate {label} id")
    for wallet in dataset.wallets.values():
        if len({instrument.instrument_id for instrument in wallet}) != len(wallet):
            raise ValueError("Duplicate instrument id in wallet")
    for request in dataset.checkouts:
        validate_checkout(request)
