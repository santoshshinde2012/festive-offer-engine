"""Serve an isolated synthetic checkout for optional real-browser verification."""

from __future__ import annotations

import argparse
import json
import signal
import tempfile
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from festive_offers.io import load_dataset
from festive_offers.online.api import ApiError, OfferApi, Principal, make_server, object_fields
from festive_offers.online.api_models import HeaderReader
from festive_offers.online.fake_provider import SYNTHETIC_EVENT_KEY, FakeProvider
from festive_offers.online.models import FrozenClock
from festive_offers.online.offer_service import OfferService
from festive_offers.online.ports import Record
from festive_offers.online.publication import prepare_bundle
from festive_offers.online.security import HmacEventVerifier
from festive_offers.online.sqlite_store import SQLiteStore
from festive_offers.ranking import ExpectedSavingsScorer

# A deliberately public fixture string; never use it outside this isolated test.
PUBLIC_FIXTURE_TOKEN = "public-browser-fixture-token-not-a-real-credential"
PUBLIC_OTHER_FIXTURE_TOKEN = "public-other-browser-fixture-not-a-real-credential"
CODE_ROOT = Path(__file__).resolve().parents[1]


class BrowserFixtureApi(OfferApi):
    """Add test-only evidence actions while exercising the real API and server."""

    def __init__(
        self, service: OfferService, store: SQLiteStore, provider: FakeProvider, user_id: str
    ) -> None:
        super().__init__(
            service,
            store,
            {
                PUBLIC_FIXTURE_TOKEN: Principal("customer", user_id),
                PUBLIC_OTHER_FIXTURE_TOKEN: Principal("customer", "other-fixture-customer"),
            },
        )
        self.store, self.provider, self.recovery = store, provider, service

    def handle(
        self, method: str, path: str, body: Any, headers: HeaderReader
    ) -> tuple[int, Record]:
        if path.startswith("/_qa/"):
            self.require(self.authenticate(headers.get("Authorization")), "customer")
            applications = self.store.rows("applications")
            if method == "GET" and path == "/_qa/status":
                return 200, {
                    "application_count": len(applications),
                    "payment_count": self.provider.payment_count(),
                    "benefit_obligation_count": len(self.store.rows("benefit_obligations")),
                    "funding_conserved": all(
                        row["conserved"] for row in self.service.report()["conservation"]
                    ),
                }
            if method == "POST" and path in {"/_qa/capture", "/_qa/benefit"}:
                object_fields(body, set())
                if len(applications) != 1:
                    raise ApiError(409, "Browser fixture expects exactly one application")
                application = applications[0]
                self.recovery.dispatch(application["application_id"])
                kind = "captured" if path == "/_qa/capture" else "cashback_confirmed"
                benefit = application["nominal_paise"] if kind == "cashback_confirmed" else None
                self.service.receive_event(
                    self.provider.event(
                        application["provider_reference"], kind, benefit_paise=benefit
                    )
                )
                return 200, asdict(self.service.inspect(application["application_id"]))
            raise ApiError(404, "Unknown browser fixture action")
        return super().handle(method, path, body, headers)


def fixture_api(root: Path) -> BrowserFixtureApi:
    """Create dedicated temporary databases, deterministic rules and one checkout."""
    fixture = CODE_ROOT / "data" / "dataset.json"
    raw = json.loads(fixture.read_text())
    store = SQLiteStore(root / "application.sqlite")
    provider = FakeProvider(root / "provider.sqlite")
    service = OfferService(
        store,
        provider,
        FrozenClock(datetime(2026, 11, 5, 12, tzinfo=timezone.utc)),
        verifier=HmacEventVerifier(SYNTHETIC_EVENT_KEY),
        scorer=ExpectedSavingsScorer(),
    )
    bundle = prepare_bundle(raw, "ui_reference_rules")
    service.prepare_snapshot(bundle, {offer["offer_id"]: 500_000 for offer in bundle["offers"]})
    service.activate_snapshot("ui_reference_rules")
    service.seed_wallets(raw["wallets"])
    checkout = replace(load_dataset(fixture).checkouts[0], request_id="reference-checkout")
    service.update_checkout(checkout, payment_id="reference-payment")
    return BrowserFixtureApi(service, store, provider, checkout.user_id)


def main() -> None:
    """Expose only loopback and clean the fixture when the browser runner exits."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ready-file", type=Path, required=True)
    args = parser.parse_args()

    def stop(signum: int, frame: Any) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    with tempfile.TemporaryDirectory(prefix="festive-browser-fixture-") as temporary:
        server = make_server(fixture_api(Path(temporary)), host="127.0.0.1", port=0)
        try:
            args.ready_file.write_text(
                json.dumps({"url": f"http://127.0.0.1:{server.server_port}"})
            )
            server.serve_forever(poll_interval=0.05)
        finally:
            server.server_close()
            args.ready_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
