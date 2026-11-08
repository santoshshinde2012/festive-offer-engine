from __future__ import annotations

from typing import Any

from ..models import SCORING_VERSION
from ..ranking import offer_problem
from ..serialization import dataset_from_dict
from .models import digest


def prepare_bundle(raw: dict[str, Any], revision: str) -> dict[str, Any]:
    """Compile a complete source snapshot while retaining rejected raw contracts."""
    if not revision or len(raw["offers"]) > 100:
        raise ValueError("A named revision and at most 100 source contracts are required")
    approved: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(raw["offers"]):
        try:
            dataset = dataset_from_dict(
                {"offers": [row], "wallets": [], "checkouts": [], "assumptions": raw["assumptions"]}
            )
            offer = dataset.offers[0]
            problem = offer_problem(offer, dataset.assumptions)
            if offer.offer_id in seen:
                raise ValueError("duplicate_offer_id")
            seen.add(offer.offer_id)
            if problem:
                raise ValueError(problem)
        except (ValueError, KeyError, TypeError) as exc:
            quarantined.append(
                {
                    "source_index": index,
                    "offer_id": str(row.get("offer_id", f"row_{index}")),
                    "reason": str(exc),
                    "source_record": row,
                }
            )
        else:
            approved.append(row)
    body = {"offers": approved, "assumptions": raw["assumptions"], "quarantined": quarantined}
    return {
        "manifest": {
            "revision": revision,
            "schema_version": 1,
            "scoring_version": SCORING_VERSION,
            "source_count": len(raw["offers"]),
            "approved_count": len(approved),
            "quarantine_count": len(quarantined),
            "content_sha256": digest(body),
        },
        **body,
    }


def verify_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    """Reject partial or modified deliveries before changing the active revision."""
    if set(bundle) != {"manifest", "offers", "assumptions", "quarantined"}:
        raise ValueError("Incomplete or unknown bundle fields")
    manifest = bundle["manifest"]
    if manifest["schema_version"] != 1 or manifest["scoring_version"] != SCORING_VERSION:
        raise ValueError("Unsupported bundle schema or scoring version")
    if (
        len(bundle["offers"]) != manifest["approved_count"]
        or len(bundle["quarantined"]) != manifest["quarantine_count"]
        or manifest["source_count"] != manifest["approved_count"] + manifest["quarantine_count"]
    ):
        raise ValueError("Incomplete rule snapshot")
    if (
        digest({key: bundle[key] for key in ("offers", "assumptions", "quarantined")})
        != manifest["content_sha256"]
    ):
        raise ValueError("Rule snapshot hash mismatch")
    dataset = dataset_from_dict(
        {
            "offers": bundle["offers"],
            "wallets": [],
            "checkouts": [],
            "assumptions": bundle["assumptions"],
        }
    )
    ids = [offer.offer_id for offer in dataset.offers]
    if len(ids) != len(set(ids)) or any(
        offer_problem(o, dataset.assumptions) for o in dataset.offers
    ):
        raise ValueError("An approved contract is invalid")
    return bundle
