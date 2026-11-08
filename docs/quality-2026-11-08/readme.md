# Local runtime verification — 8 November 2026

This record covers Festive Offer Engine, its browser client, notebooks and result
tools. Static and tooling checks have been refreshed for the standalone
repository. PostgreSQL and browser observations below are retained from before
extraction; relocating the source does not repeat those executions. Each raw
transcript and capture manifest retains its own run identity and source hashes.

## Observed checks

| Check | Observed result | Evidence |
|---|---|---|
| Standalone Ruff lint and formatting | 69 local Python files passed | [Quality gate](quality-gate.txt) |
| Standalone mypy, Python 3.10 target | No errors in the 45-module runtime package | [Quality gate](quality-gate.txt) |
| Standalone naming and dependency boundaries | Module names and domain-to-adapter import rules passed | [Quality gate](quality-gate.txt) |
| Standalone evidence/package/dependency tooling | **22 passed**, including a package-root isolation check | [Transcript](tests-tools.txt) |
| Retained isolated PostgreSQL observation | **121 passed**, no skips; PostgreSQL 16.2, psycopg 3.3.8, pgserver 0.1.4 | [Transcript](tests-postgres.txt) |
| Default dependency-free suite | **99 passed; 22 PostgreSQL integrations explicitly skipped** | [Completed capture](evidence/evidence_manifest.json), [transcript](evidence/run.log) |
| Retained checkout browser observation | **33 Chrome checks passed**; 51 source/input files bound before and after | [Browser record](ui/ui-verification.json), [desktop](ui/checkout-desktop-owed.png), [mobile](ui/checkout-mobile-fulfilled.png) |
| Local HTTP workflow | Ten requests, one fake payment, exact retry reuse, OWED then separate FULFILLED, matching funding/journal | [Summary](runtime_summary.json), [HTTP responses](http_steps.json) |
| Earlier captures | November 6 and 7 saved evidence verifies without changing its recorded results | [Original check](original-evidence-check.txt), [November 7 check](nov7-evidence-check.txt) |

The completed capture records its source/configuration identity and syntax-check
inventory. Verification checks 25 artifact-byte hashes, 15 canonical JSON
exports, all 13 operational tables and financial replay. Requesting PostgreSQL
tests is recorded separately from observing them execute; per-test statuses
remain in the transcript. No Lakebase connection or Databricks workspace run is
claimed.

Historical captures remain in their dated folders. Generated run directories,
caches, bytecode and local virtual environments are rebuildable and are not
retained runtime evidence.

## Contracts checked

- Trusted wallet ingestion rejects duplicate instrument IDs before persistence,
  so ranking and payment binding cannot choose different records under one ID.
- Both stores share one export serializer. Unsafe report paths and collisions
  with table/manifest names are rejected before filesystem output.
- The fake provider serializes submission and event sequencing. A concurrent
  changed binding cannot be acknowledged as the original payment.
- Publication clocks are sampled after lock acquisition; duration configuration
  is validated before constructing use cases.
- Fresh SQLite constraints enforce money/limit invariants. Existing databases
  are not silently migrated by `CREATE TABLE IF NOT EXISTS`.
- Customer changes clear previous UI selections and status. An ambiguous retry
  retains its original checkout, credential, offer and idempotency key.
- Invalid adapter configuration fails before database creation. Private runtime
  environment files are excluded from Git, bundle synchronization and packages.
- The source-package command defaults to this repository, so neighboring
  workspace files cannot enter an archive through its default root.

## Maintained boundaries

Canonical modules identify the adapters and facade: `fake_provider.py`,
`sqlite_store.py`, `postgres_store.py` and `offer_service.py`. The old
provider/store/service paths remain aliases of the same classes.

Financial use cases depend on protocols. One unit of work keeps financial
changes atomic; read-only interface properties expose only the required
facets. Authentication, customer-owned resource access, role endpoints,
configuration, adapter construction and command operation remain separate.
Evidence readers use typed source/syntax records, and rendering consumes one
loaded report context. The [architecture guide](../architecture.md) explains
these contracts.

## Repeat the checks

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
./verify_quality.sh
./run_demo.sh
./run_runtime_demo.sh
python -m unittest discover -s tools/tests -v
./record_results.sh --verify-run docs/quality-2026-11-08/evidence
```

For an isolated PostgreSQL run:

```bash
python -m pip install -r requirements-test-postgres.txt
FESTIVE_TEST_POSTGRES_DSN='' FESTIVE_TEST_EMBEDDED_POSTGRES=1 PYTHONPATH=src python -m unittest discover -s tests -v
```

[Browser instructions](../browser-qa.md) describe optional Chrome checks. The
screenshots are actual local UI captures with synthetic rules and observations.
They are not vendor screenshots or proof of real payments, cloud deployment or
production performance. Passing these checks does not verify every future
adapter or extension.

## Standalone extraction check

The fresh `standalone_20261108` capture binds 89 source/configuration files and syntax-checks 69 Python files. A temporary isolated copy also passed static checks, all 22 tooling tests and the complete application scenario. [Isolation transcript](standalone-isolation.txt). The HTTP summary and responses above were refreshed from the standalone repository. The Medium article URL will be added after publication; no local article path is needed to run this source.
