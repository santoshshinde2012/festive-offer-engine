"""Exercise real HTTP requests and restartable workers without external services."""

from __future__ import annotations

import argparse
import json
import platform
import secrets
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from .api import OfferApi, Principal, make_server
from .api_models import Role
from .models import canonical, digest
from .publication import prepare_bundle
from .runtime import build_runtime
from .worker import Worker


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the synthetic HTTP/API/worker reference end to end"
    )
    parser.add_argument("--output", type=Path, default=Path("output"))
    parser.add_argument(
        "--run-id", default=datetime.now(timezone.utc).strftime("runtime_%Y%m%dT%H%M%S_%fZ")
    )
    args = parser.parse_args()
    destination = args.output / args.run_id
    destination.mkdir(parents=True, exist_ok=False)
    raw = json.loads((Path(__file__).resolve().parents[3] / "data/dataset.json").read_text())
    checkout = raw["checkouts"][0]
    bundle = prepare_bundle(raw, "runtime_synthetic_v1")
    env = {
        "FESTIVE_STORE": "sqlite",
        "FESTIVE_PROVIDER": "fake",
        "FESTIVE_SQLITE_PATH": str(destination / "application.sqlite"),
        "FESTIVE_PROVIDER_DB": str(destination / "provider.sqlite"),
        "FESTIVE_SYNTHETIC_NOW": "2026-11-05T12:00:00+00:00",
    }
    runtime = build_runtime(env)
    roles: tuple[Role, ...] = ("operator", "merchant", "customer")
    tokens = {role: secrets.token_urlsafe(32) for role in roles}
    identities = {
        tokens[role]: Principal(role, checkout["user_id"] if role == "customer" else None)
        for role in tokens
    }
    server = make_server(OfferApi(runtime.service, runtime.store, identities), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    steps: list[dict] = []

    def call(
        path: str, body: dict | None = None, *, role: Role = "customer", key: str | None = None
    ) -> dict:
        headers = {"Authorization": f"Bearer {tokens[role]}", "Content-Type": "application/json"}
        if key:
            headers["Idempotency-Key"] = key
        request = Request(
            base + path,
            data=canonical(body).encode() if body is not None else None,
            headers=headers,
        )
        with urlopen(request, timeout=5) as response:
            value = json.load(response)
            steps.append(
                {
                    "method": request.get_method(),
                    "path": path,
                    "status": response.status,
                    "response": value,
                }
            )
            return value

    try:
        call(
            "/v1/operator/publications",
            {"bundle": bundle, "funding_paise": {o["offer_id"]: 500000 for o in bundle["offers"]}},
            role="operator",
        )
        call("/v1/operator/publications/runtime_synthetic_v1/activate", {}, role="operator")
        call("/v1/merchant/wallets", {"wallets": raw["wallets"]}, role="merchant")
        call(
            "/v1/merchant/checkouts",
            {"checkout": checkout, "payment_id": "runtime_merchant_order_1"},
            role="merchant",
        )
        quote = call("/v1/quotes", {"checkout_id": checkout["request_id"]})
        selection = {"quote_id": quote["quote_id"], "offer_id": "diwali_b_cashback"}
        app = call("/v1/applications", selection, key="selection_1")
        retry = call("/v1/applications", selection, key="selection_1")
        assert retry["application_id"] == app["application_id"]
        # New composition roots read durable work; no shared in-memory queue.
        dispatch_runtime = build_runtime(env)
        dispatched = Worker(dispatch_runtime.service, dispatch_runtime.store).run_once()
        provider = dispatch_runtime.simulated_provider
        assert provider is not None
        provider.event(app["provider_reference"], "captured")
        recovered_runtime = build_runtime(env)
        captured = Worker(recovered_runtime.service, recovered_runtime.store).run_once()
        owed = call(f"/v1/applications/{app['application_id']}")
        assert owed["benefit_state"] == "OWED"
        provider.event(app["provider_reference"], "cashback_confirmed", benefit_paise=120000)
        fulfilled = Worker(recovered_runtime.service, recovered_runtime.store).run_once()
        final = call(f"/v1/applications/{app['application_id']}")
        assert final["benefit_state"] == "FULFILLED"
        report = call("/v1/operator/report", role="operator")
        assert all(row["conserved"] for row in report["conservation"])
        assert all(row["matches_native_state"] for row in report["replay_budget_reconciliation"])
        runtime.store.export(
            destination / "exports",
            {"replay_report": report, "reconciliation_report": report["reconciliation_required"]},
        )
        summary = {
            "scope": "real loopback HTTP, SQLite, restartable workers, fake provider; no Databricks or real payment",
            "python": platform.python_version(),
            "http_requests": len(steps),
            "duplicate_reused": True,
            "maximum_reserved_paise": app["nominal_paise"],
            "capture_state": owed["benefit_state"],
            "final_state": final["benefit_state"],
            "provider_payments": provider.payment_count(),
            "worker_runs": [asdict(value) for value in (dispatched, captured, fulfilled)],
            "funding_conserved": True,
            "journal_matches_native_state": True,
        }
        for filename, value in (("http_steps.json", steps), ("runtime_summary.json", summary)):
            (destination / filename).write_text(json.dumps(value, indent=2) + "\n")
        (destination / "run_manifest.json").write_text(
            canonical(
                {
                    "completed": True,
                    "summary_sha256": digest(summary),
                    "http_steps_sha256": digest(steps),
                }
            )
            + "\n"
        )
        print(json.dumps(summary, indent=2))
        print(f"Evidence: {destination.resolve()}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
