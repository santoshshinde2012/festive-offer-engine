# Observed runtime completion

This is a retained local observation from before standalone extraction, verified
on 7 November 2026 using Python 3.12.6. Run reproduction commands from the
repository root. Original source hashes and recorded paths describe that
historical run. These records extend the [original retained scenario](../results/readme.md); they do not replace its historical 53-test capture.

| Check | Observed result | Record |
|---|---|---|
| `./run_demo.sh` without optional PostgreSQL opt-in | 77 passed; 14 explicit PostgreSQL skips; original synthetic scenario passed | [Transcript](tests-standard-library.txt) |
| `FESTIVE_TEST_EMBEDDED_POSTGRES=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v` | All 91 passed, no skips; actual PostgreSQL 16.2, psycopg 3.3.8, pgserver 0.1.4 | [Combined transcript](tests-postgres-combined.txt) |
| `./run_runtime_demo.sh` | 10 actual HTTP requests; same-key retry reused one application; separate worker compositions recovered OWED then FULFILLED; one fake payment; conserved funding and matching journal | [Summary](runtime_summary.json), [HTTP responses](http_steps.json), [completion marker](run_manifest.json) |
| `./run_local.sh` | Actual API process and separate worker process; health and three UI assets served; CLI simulator capture → OWED, separate payout → FULFILLED; budget stayed COMMITTED; Ctrl-C stopped both processes | [Verification record](verification.json) |
| Optional Chrome/Playwright UI run | 21 checks passed at 1280px desktop and 390px mobile, including retry after a deliberately lost accepted response | [Browser manifest](qa/ui-verification.json), [desktop](qa/checkout-desktop-owed.png), [mobile](qa/checkout-mobile-fulfilled.png) |
| `python3 -m unittest discover -s tools/tests -v` | Twelve evidence-tool tests passed | [Transcript](tests-evidence-tools.txt) |
| `./record_results.sh --verify-run docs/results/evidence` | Original 25 byte hashes, 15 canonical hashes, all 13 tables and replay/funding checks passed | [Verification output](retained-evidence-check.txt) |

The actual PostgreSQL checks include simultaneous customer/budget contention, stable payment identity, BaseException rollback, deferred/reordered benefit evidence, expiry, unknown-provider recovery, bounded work pages, export hashes, 64-bit amounts, lock/statement timeouts and an HTTP → PostgreSQL → worker lifecycle. The adapter deliberately serializes the reference with a schema advisory lock; this is no throughput measurement.

A [fresh completed capture](completed-evidence/evidence_manifest.json) also records the accurate 77-passed/14-skipped result and hashes all 69 source/configuration files, including the web UI, deployment YAML and dependency files. Its read-only verification checks 25 file hashes, 15 canonical exports, all 13 tables and matching funding/journal replay. The original saved capture still verifies unchanged.

The provider-transport tests use a real loopback HTTP fixture and the normalized contract, not a payment network. They exercise timeout after acceptance, original-reference recovery, authentication, forged evidence, response limits, invalid envelopes and redirect refusal. The seven Lakebase connector tests inject credential/connection clients; an installed SDK 0.89.0 signature check additionally verified the expected API without calling it.

The [export directory](exports/export_manifest.json) contains all 13 operational tables and replay/reconciliation reports from the HTTP run. Canonical hashes cover JSON values; they are not signatures or file-byte hashes. The [verification record](verification.json) separately retains byte hashes for the selected source and evidence files. Private launcher credentials and temporary database files are excluded from this evidence set.

No Databricks workspace validation, deployment, job execution or Lakebase connection is claimed. Job YAML was checked offline against the installed CLI 0.269.0 schema. Managed synchronization and real bank/issuer adapters still need external integration work. The runnable baseline uses approved file bundles and analytical batch exports.

See [runtime guide](../runtime.md) for reproducible commands and [the optional browser harness](../browser-qa.md) for the retained browser check source.
