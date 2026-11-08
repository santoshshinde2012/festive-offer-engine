"""Named CLI commands for service operation and explicit synthetic actions."""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import threading
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

from ..serialization import dataset_from_dict
from ..validation import validate_dataset
from .api import OfferApi, make_server
from .publication import verify_bundle
from .runtime_factory import build_runtime, configured_tokens
from .worker import Worker

Command = Callable[[argparse.Namespace, argparse.ArgumentParser], None]


def _emit(value: object) -> None:
    print(json.dumps(value), flush=True)


def run_serve(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Validate identity and listener settings before opening runtime resources."""
    if not 0 <= args.port <= 65535:
        parser.error("Port must be between 0 and 65535")
    tokens = configured_tokens()
    runtime = build_runtime()
    api = OfferApi(runtime.service, runtime.store, tokens)
    with make_server(api, port=args.port) as server:
        _emit(
            {
                "url": f"http://127.0.0.1:{server.server_port}",
                "provider": os.environ.get("FESTIVE_PROVIDER", "fake"),
            }
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


def run_worker(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Keep recovery alive across transient cycles, retaining one-shot failures."""
    if not math.isfinite(args.interval) or not 0.1 <= args.interval <= 3600:
        parser.error("Worker interval must be finite and between 0.1 and 3600 seconds")
    if not 1 <= args.batch_size <= 1000:
        parser.error("Worker batch size must be between 1 and 1000")
    runtime = build_runtime()
    worker = Worker(runtime.service, runtime.store, batch_size=args.batch_size)
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    while not stop.is_set():
        result: dict[str, Any]
        try:
            result = asdict(worker.run_once())
        except Exception as exc:
            if args.once:
                raise
            result = {"cycle_error": type(exc).__name__, "retry_after_seconds": args.interval}
        _emit(result)
        if args.once:
            break
        stop.wait(args.interval)


def run_publish(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Read one approved artifact before preparing its immutable revision."""
    if args.customer_limit < 1:
        parser.error("Customer limit must be positive")
    bundle = verify_bundle(json.loads(args.bundle.read_text(encoding="utf-8")))
    funding = json.loads(args.funding.read_text(encoding="utf-8"))
    if not isinstance(funding, dict):
        parser.error("Funding must map approved offer IDs to integer paise")
    runtime = build_runtime()
    revision = runtime.service.prepare_snapshot(bundle, funding, args.customer_limit)
    if args.activate:
        runtime.service.activate_snapshot(revision)
    _emit({"revision": revision, "activated": args.activate})


def run_activate(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Activate a prepared revision without rewriting its contents."""
    if not args.revision:
        parser.error("A prepared revision is required")
    runtime = build_runtime()
    runtime.service.activate_snapshot(args.revision)
    _emit({"revision": args.revision, "activated": True})


def run_seed(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Validate one trusted input snapshot before opening the operational database."""
    raw = json.loads(args.data.read_text(encoding="utf-8"))
    dataset = dataset_from_dict(raw)
    validate_dataset(dataset)
    runtime = build_runtime()
    runtime.service.seed_wallets(raw["wallets"])
    for checkout in dataset.checkouts:
        runtime.service.update_checkout(checkout)
    _emit({"wallets": len(dataset.wallets), "checkouts": len(dataset.checkouts)})


def run_export(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Write a fresh operational snapshot without replacing existing evidence."""
    if args.destination.exists():
        parser.error("Export destination already exists")
    runtime = build_runtime()
    report = runtime.service.report()
    manifest = runtime.store.export(
        args.destination,
        {
            "replay_report": report,
            "reconciliation_report": report["reconciliation_required"],
        },
    )
    _emit(manifest)


def run_simulate(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Emit a fake observation for a worker to read without editing the ledger."""
    if os.environ.get("FESTIVE_PROVIDER", "fake") != "fake":
        parser.error("simulate requires FESTIVE_PROVIDER=fake")
    runtime = build_runtime()
    provider = runtime.simulated_provider
    if provider is None:
        parser.error("simulate requires FESTIVE_PROVIDER=fake")
    application = runtime.service.inspect(args.application_id)
    message = provider.event(
        application.provider_reference, args.kind, benefit_paise=args.benefit_paise
    )
    _emit(
        {
            "event_id": message.payload["event_id"],
            "scope": "synthetic observation; run worker to ingest",
        }
    )


def build_parser() -> argparse.ArgumentParser:
    """Associate each command with one handler, preserving the public CLI."""
    parser = argparse.ArgumentParser(
        description="Festive offers reference runtime; fake payments by default"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Run authenticated loopback HTTP API")
    serve.add_argument("--port", type=int, default=8080)
    serve.set_defaults(handler=run_serve)
    worker = commands.add_parser("worker", help="Dispatch and reconcile durable work")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--interval", type=float, default=2.0)
    worker.add_argument("--batch-size", type=int, default=100)
    worker.set_defaults(handler=run_worker)
    publish = commands.add_parser("publish", help="Verify and prepare an approved serving bundle")
    publish.add_argument("--bundle", type=Path, required=True)
    publish.add_argument("--funding", type=Path, required=True)
    publish.add_argument("--customer-limit", type=int, default=1)
    publish.add_argument("--activate", action="store_true")
    publish.set_defaults(handler=run_publish)
    activate = commands.add_parser("activate", help="Activate a prepared complete revision")
    activate.add_argument("revision")
    activate.set_defaults(handler=run_activate)
    seed = commands.add_parser("seed", help="Ingest a trusted synthetic dataset")
    seed.add_argument("--data", type=Path, required=True)
    seed.set_defaults(handler=run_seed)
    export = commands.add_parser("export", help="Write a new operational snapshot for Delta import")
    export.add_argument("--destination", type=Path, required=True)
    export.set_defaults(handler=run_export)
    simulate = commands.add_parser("simulate", help="Emit one explicitly fake provider observation")
    simulate.add_argument("application_id")
    simulate.add_argument(
        "kind",
        choices=[
            "captured",
            "failed",
            "unknown",
            "refunded",
            "cashback_confirmed",
            "instant_discount_confirmed",
        ],
    )
    simulate.add_argument("--benefit-paise", type=int)
    simulate.set_defaults(handler=run_simulate)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    handler = cast(Command, args.handler)
    handler(args, parser)


if __name__ == "__main__":
    main()
