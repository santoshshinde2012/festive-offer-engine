# Festive Offer Engine

An end-to-end offer reference that uses fictional E-Shop inputs, SQLite and a durable fake payment provider. No real payment is sent.

The flow validates rules, ranks eligible offers, reserves campaign funding, processes a simulated payment, records cashback owed, confirms delivery separately and exports replayable evidence. PostgreSQL is optional. Databricks notebook/jobs and a Lakebase connector are supplied; cloud execution remains unverified.

## Run locally

Use Python 3.10+ and Bash. The default example needs no packages or credentials. Clone the repository, then run from its root:

```bash
git clone https://github.com/santoshshinde2012/festive-offer-engine.git
cd festive-offer-engine
```

```bash
./run_demo.sh                          # tests and complete scenario
./run_runtime_demo.sh                  # HTTP API and worker scenario
./run_local.sh                         # interactive checkout
```

Each run writes a fresh directory under `output/`. To run only the scenario, use `./run_end_to_end.sh --run-id scenario_01`. Reusing a run ID is rejected. The fixture's fixed UTC server clock keeps its invented campaign windows reproducible. Its ₹5,000 campaign funding and `0.80` cashback probability are synthetic assumptions.

## Expected result

A ₹10,000 eligible basket produces a ₹750 instant discount and ₹1,200 possible cashback. Expected-value ranking scores the cashback at ₹960, but admission reserves the full ₹1,200 liability. Capture creates an `OWED` benefit; separate matching delivery evidence makes it `FULFILLED`.

The scenario checks exact retries, definitive-failure release, uncertain-payment recovery and `funding = available + held + committed`. Output includes source inputs, an approved serving bundle, local databases, scenario summary, 13 exported operational tables, reconciliation/replay reports and completion manifests.

## Guides

- [Architecture and SOLID contracts](docs/architecture.md): component responsibilities and extension boundaries.
- [Runtime](docs/runtime.md): checkout, authenticated API, worker, PostgreSQL and provider configuration.
- [Databricks](docs/databricks.md): notebook/jobs, serving-bundle handoff, analytical import and Lakebase setup.
- [Browser checks](docs/browser-qa.md): optional Chrome desktop/mobile verification.
- [Current verification](docs/quality-2026-11-08/readme.md): exact commands, results and execution limits.

The [original results](docs/results/readme.md) and [earlier runtime capture](docs/runtime/readme.md) retain historical test counts and raw evidence. [Open the original HTML report](docs/results/index.html) for its readable checkpoints.

## Tests and static checks

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m unittest discover -s tools/tests -v
```

The default application suite passes 99 tests and explicitly skips 22 optional PostgreSQL tests. The [runtime guide](docs/runtime.md#provider-adapter-and-verification) explains real PostgreSQL opt-in. Its retained 121-test run is a separate observation. Tooling, browser and visual checks have separate results.

Optional static checks:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
./verify_quality.sh
```

## Capture and verify evidence

Use a fresh run ID and destination:

```bash
./record_results.sh --run-id example_01 --destination output/example/evidence
./record_results.sh --verify-run output/example/evidence
python3 -m tools.package_review
```

Add `--snapshots` to capture the report with locally installed Chrome/Chromium. Saved-run verification reads retained records and hashes without opening a database, dispatching a payment or rerunning tests. Preserve old manifests with their original source identities.

Generated `output/`, caches, bytecode and `.venv` are disposable. Retained `docs/` evidence is used by verification tests. Private environment files and generated databases are excluded from the review archive.
