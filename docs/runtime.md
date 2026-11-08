# Run the checkout service, worker and storage adapters

The reference now includes a customer UI, an authenticated HTTP API, a separately runnable worker, SQLite and PostgreSQL stores, a normalized HTTP provider adapter, and Databricks job definitions. Local payments and payout receipts are synthetic. The cloud jobs and Lakebase connection have not been run in a Databricks workspace.

## One-command demonstrations

From the repository root, with Python 3.10+:

```bash
./run_runtime_demo.sh
```

This creates a fresh output directory and makes actual loopback HTTP requests. It prepares and activates approved rules, ingests trusted wallet/basket records, obtains a quote and applies it twice with the same key. Separately composed workers dispatch the durable command, recover capture as `OWED`, then ingest a separate payout as `FULFILLED`. It exports all 13 tables and a funding/journal reconciliation. `http_steps.json`, `runtime_summary.json` and the final completion manifest retain the observation without bearer credentials.

To use the customer screen interactively:

```bash
./run_local.sh
```

Open `http://127.0.0.1:8080`. Enter the generated customer token and checkout ID printed in the terminal. The launcher creates a fresh synthetic database and a private `runtime.env` file under `output/`, publishes the fixture, and starts the API and a separate worker process. It does not open a browser or send a real payment. Stop it with Ctrl-C; the launcher also stops its worker. Restarting this script creates another independent run.

The screen compares the ₹1,200 nominal cashback with its assumed ₹960 expected value. Applying reserves the full ₹1,200. After dispatch, the screen remains pending until the simulated external system supplies facts. To create those facts, use a second terminal, sourcing the **exact newly generated configuration path** printed by the launcher:

```bash
source /absolute/path/to/output/interactive_RUN/runtime.env
python3 -m festive_offers.online.runtime simulate APPLICATION_ID captured
```

The application ID appears in the customer screen. After the worker's next cycle, refresh status: cashback is owed. Then supply the separate receipt:

```bash
python3 -m festive_offers.online.runtime simulate APPLICATION_ID cashback_confirmed --benefit-paise 120000
```

Refresh again after the next worker cycle to see fulfillment. Emitting a fake receipt is separate from receiving it; the simulator never edits the application ledger. Funding remains committed after fulfillment. Keep this configuration local: it contains a generated reference credential. `runtime.env` is excluded from Git, bundle synchronization and review ZIPs, including custom destinations. Remove the selected `output/interactive_RUN` directory after stopping both processes to clean up.

## Run processes separately

`PYTHONPATH` must contain the absolute `src/` directory. The launcher-generated environment supplies this. To restart that same local run, source its `runtime.env`, then use separate terminals:

```bash
python3 -m festive_offers.online.runtime serve --port 8080
python3 -m festive_offers.online.runtime worker --interval 2 --batch-size 100
```

Use `worker --once` for one bounded cycle. Continuous workers keep polling after a transient cycle-level database error; `--once` exits with the error. Every dispatch claim commits before the external request. Two workers may discover the same READY item, but the guarded transaction allows one dispatch. Only undispatched holds expire. Claimed, pending and unknown payments are reconciled through their original reference; a missing event never releases money or triggers a replacement payment. The worker also revisits unpaid benefit obligations. Recovery pages rotate so an unresolved early item does not starve later items. A crash after claiming but before submission can require operator review if the provider has no definitive evidence.

## Authenticated API

`FESTIVE_AUTH_TOKENS_JSON` maps configured bearer tokens to identity objects. Tokens must contain 24–4096 visible ASCII characters. A customer identity also requires `user_id`. The interactive launcher creates only a customer token; publication and ingestion happen through its trusted CLI. To use administrative HTTP routes, configure separate tokens with the roles below. Tokens stay in process configuration, never in request-body identity fields.

| Caller | Endpoint | Input or result |
|---|---|---|
| Public | `GET /health`, `GET /` | Health and static customer screen |
| Customer | `POST /v1/quotes` | `{"checkout_id":"..."}`; basket and wallet loaded from trusted records |
| Customer | `POST /v1/applications` | `{"quote_id":"...","offer_id":"..."}` plus `Idempotency-Key`; payment identity loaded on the server |
| Customer | `GET /v1/applications/{id}` | Only the authenticated owner's application |
| Merchant | `POST /v1/merchant/wallets` | `{"wallets":[...]}`; verified synthetic instrument metadata |
| Merchant | `POST /v1/merchant/checkouts` | `{"checkout":{...},"payment_id":"..."}` |
| Operator | `POST /v1/operator/publications` | `bundle`, `funding_paise`, optional `customer_limit` |
| Operator | `POST /v1/operator/publications/{revision}/activate` | Empty JSON object |
| Operator | `POST /v1/operator/campaigns/{offer_id}` | `{"enabled":false}` |
| Operator | `GET /v1/operator/report` | Funding, journal replay, unresolved payments and owed benefits |
| Provider | `POST /v1/provider/events` | `{"payload":{...},"signature":"..."}`; bearer role and HMAC both required |

Cross-customer checkout, quote and application identifiers return not found. Customers cannot replace merchant facts or funding. JSON bodies have a 256 KiB limit; duplicate JSON fields and ambiguous request framing are rejected. The UI retains a retry's idempotency key, checkout and credential after an uncertain response. Switching a checkout or customer clears previous selections/status before loading the new context. Pending retry identity is frozen, and the response must match that request. The credential is never written to browser storage. A page reset does not cancel an accepted application. To recover after closing the page, use the recorded application ID; the API can read its durable status.

This is a loopback reference server based on the Python standard library. External deployment needs a supported HTTP runtime or gateway, TLS, managed identity/session authentication, operating controls and the actual provider contract. Static reference bearer credentials do not establish a production identity system.

## PostgreSQL and Lakebase

The default SQLite run needs no installed packages. To run the same services against a dedicated PostgreSQL database:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-postgres.txt
export FESTIVE_STORE=postgres
export FESTIVE_POSTGRES_DSN='host=127.0.0.1 dbname=your_demo_db user=your_demo_user'
export FESTIVE_POSTGRES_SCHEMA=festive_offers_demo
export FESTIVE_POSTGRES_INITIALIZE=true
```

Provide the connection secret through your chosen credential mechanism rather than source files. Initial construction creates only this dedicated schema and its tables. Once initialized by the owner, set `FESTIVE_POSTGRES_INITIALIZE=false` for a separately granted runtime role. All runtime processes must select the same store/schema/provider configuration. Publishing a new rule revision cannot reset campaign funding or customer usage.

The PostgreSQL implementation uses a transaction advisory lock for its schema **before** the service reads current time or mutable records. All writers must follow this protocol. It deliberately serializes the reference workflow and is not a throughput benchmark. Monetary columns use `BIGINT`; statement, lock and connection timeouts are bounded. The portable export takes one protected table snapshot, with its completion manifest written last. Reports declare that they were read before export.

For Lakebase Autoscaling, install `requirements-databricks.txt`, select `FESTIVE_STORE=lakebase`, and configure the endpoint/PG coordinates described in [databricks.md](databricks.md). Each fresh connection obtains a fresh OAuth database credential using the Databricks SDK; the store owns and closes that connection. Local injection tests verify this credential lifecycle. A successful Lakebase connection remains unobserved.

## Publication and analytical return path

The bundle jobs in [databricks.md](databricks.md) prepare approved/quarantined Delta records and publish a complete portable artifact. After downloading a trusted `serving_bundle.json`, prepare and activate its exact revision:

```bash
python3 -m festive_offers.online.runtime publish --bundle /absolute/serving_bundle.json --funding /absolute/funding.json --activate
python3 -m festive_offers.online.runtime export --destination /absolute/new_operational_export
```

The funding JSON maps **every approved offer ID** to authorized integer paise; it is not inferred from sales traffic or the ranking score. Omit `--activate` to prepare first, then use `runtime activate REVISION`. A duplicate revision fails; inspect the existing prepared revision rather than changing an immutable record. Upload the new export directory to the configured volume and run the separate `import_operations` job. Completion markers exclude partial analytical writes. Managed synced-table provisioning is an optional target architecture path; this implemented baseline delivers verified file bundles and batch exports.

## Provider adapter and verification

`FESTIVE_PROVIDER=fake` is the default. `FESTIVE_PROVIDER=http` requires `FESTIVE_PROVIDER_URL`, `FESTIVE_PROVIDER_TOKEN`, and a separate `FESTIVE_EVENT_KEY` of at least 32 bytes. It also disables the synthetic frozen-clock setting. The configured HTTPS service must implement the normalized contract documented in [`http_provider.py`](../src/festive_offers/online/http_provider.py): stable-reference POST acknowledgement and signed ordered event history. There are no automatic payment retries or redirects. Timeouts mean uncertainty; evidence lookup resolves the original reference. This adapter is not a bank, Stripe or issuer integration. Payment and benefit evidence from actual partners still need their own authenticated mapping and deployment verification.

The regular suite runs without optional packages. To exercise the actual PostgreSQL engine in a temporary isolated directory:

```bash
python -m pip install -r requirements-test-postgres.txt
FESTIVE_TEST_EMBEDDED_POSTGRES=1 PYTHONPATH=src python -m unittest discover -s tests -v
```

Alternatively set `FESTIVE_TEST_POSTGRES_DSN` to an explicitly dedicated test database. Each test creates and removes its own random schema. Never point this option at an ordinary application schema. [Browser QA instructions](browser-qa.md) cover the optional desktop/mobile checks. Current observed results and scope are retained in [docs/quality-2026-11-08/readme.md](quality-2026-11-08/readme.md); the earlier [runtime completion record](runtime/readme.md) remains historical evidence.
