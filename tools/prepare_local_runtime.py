"""Prepare a new, private configuration for the explicitly synthetic UI runtime."""

from __future__ import annotations

import argparse
import json
import secrets
import shlex
from datetime import datetime, timezone
from pathlib import Path

from festive_offers.online.publication import prepare_bundle


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    destination = (
        args.destination
        or root / "output" / datetime.now(timezone.utc).strftime("interactive_%Y%m%dT%H%M%S_%fZ")
    ).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    destination.chmod(0o700)
    raw = json.loads((root / "data/dataset.json").read_text())
    bundle = prepare_bundle(raw, "interactive_synthetic_v1")
    (destination / "bundle.json").write_text(json.dumps(bundle, indent=2) + "\n")
    (destination / "funding.json").write_text(
        json.dumps({row["offer_id"]: 500000 for row in bundle["offers"]}, indent=2) + "\n"
    )
    customer_token = secrets.token_urlsafe(32)
    env = {
        "PYTHONPATH": str(root / "src"),
        "FESTIVE_STORE": "sqlite",
        "FESTIVE_PROVIDER": "fake",
        "FESTIVE_SQLITE_PATH": str(destination / "application.sqlite"),
        "FESTIVE_PROVIDER_DB": str(destination / "provider.sqlite"),
        "FESTIVE_SYNTHETIC_NOW": "2026-11-05T12:00:00+00:00",
        "FESTIVE_AUTH_TOKENS_JSON": json.dumps(
            {customer_token: {"role": "customer", "user_id": raw["checkouts"][0]["user_id"]}}
        ),
    }
    configuration = destination / "runtime.env"
    configuration.touch(mode=0o600)
    configuration.write_text(
        "\n".join(f"export {key}={shlex.quote(value)}" for key, value in env.items()) + "\n"
    )
    # The launcher sources the configuration; it prints only this path here.
    print(configuration)


if __name__ == "__main__":
    main()
