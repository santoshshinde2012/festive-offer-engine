# Databricks preparation and operational analytics

The primary notebook is [`notebooks/end_to_end_pipeline.py`](../notebooks/end_to_end_pipeline.py). It connects the same publication validator and ranking kernel used by the local application to Delta storage and a portable serving artifact.

The complete data loop is:

```text
synthetic source dataset
  → shared contract validator
  → raw / approved / quarantined Delta records
  → immutable manifest + serving_bundle.json in a Unity Catalog volume
  → local application verifies, prepares, and activates that exact bundle
  → recommendation → application → fake payment → benefit obligation
  → consistent operational export + export_manifest.json
  → Delta operational records → reconciliation SQL
```

The default application uses SQLite transactions and a fake provider. The engine also includes a PostgreSQL store and an optional Lakebase Autoscaling connection factory. The notebook prepares rules and imports evidence; the application ledger runs through its own database connection. No Databricks account execution, Lakebase connection, or real provider request was performed during authoring. Local checks cannot establish workspace privileges, managed sync behavior, Lakebase recovery, or production throughput.

## Prepare the workspace

Place this repository in Workspace files or a Databricks Git folder, keeping `src/`, `data/`, and `notebooks/` together. Use an absolute path such as `/Workspace/Users/you@example.com/festive-offer-engine`; relative working directories are not assumed.

Choose a demo catalog that you can write to. The notebook creates its dedicated `festive_offer_e2e` schema. Create an artifact volume before running it, for example by replacing `demo_catalog` below with that catalog:

```sql
CREATE SCHEMA IF NOT EXISTS demo_catalog.festive_offer_e2e;
CREATE VOLUME IF NOT EXISTS demo_catalog.festive_offer_e2e.artifacts;
```

The executing identity needs `USE CATALOG`, `USE SCHEMA`, and `CREATE TABLE`, plus `SELECT`/`MODIFY` on existing demo tables. Creating the schema or volume requires the corresponding `CREATE SCHEMA` or `CREATE VOLUME` privilege. Reading/writing artifacts requires `READ VOLUME`/`WRITE VOLUME` on the volume and access to its parent catalog/schema.

Import `notebooks/end_to_end_pipeline.py` as a Python source notebook and attach Unity Catalog-compatible compute supporting Python 3.10+ and the DataFrame/Delta APIs used here. Do not install standalone `pyspark` into the workspace environment. Its shared publication/ranking modules need no additional packages.

## Deploy the two on-demand jobs

[`databricks.yml`](../databricks.yml) and [`deployment/jobs.yml`](../deployment/jobs.yml) define a [Declarative Automation Bundle](https://docs.databricks.com/aws/en/dev-tools/bundles/settings) with separate `publish_rules` and `import_operations` jobs. Both use the same notebook and shared Python package. Neither has a schedule or trigger. Deploying the bundle creates job definitions; `bundle run` starts compute and performs writes.

From the repository root, using your deliberately selected authenticated workspace/profile:

```bash
export BUNDLE_VAR_catalog="demo_catalog"
export BUNDLE_VAR_export_volume="/Volumes/demo_catalog/festive_offer_e2e/artifacts"
databricks bundle validate -t dev
databricks bundle deploy -t dev
databricks bundle run -t dev publish_rules -- --publication_id=publication_cloud_01 --business_revision=e_shop_campaigns_v1
```

These are operator commands to run in your workspace; they were not executed during authoring. The catalog and volume must already exist, as described above. The jobs use serverless notebook compute, so that capability must be available in your workspace. For classic compute, explicitly add your approved `existing_cluster_id` or job-cluster configuration to both tasks before deployment.

The bundle deploys `src/`, `data/`, and the notebook together. Its `repo_root` job parameter uses `${workspace.file_path}`, pointing to the deployed bundle directory; the notebook adds that directory's `src/` to `sys.path`. It therefore imports the deployed validator/ranker, rather than relying on the developer's local working directory or a separately installed package. Local database files, generated result evidence, virtual environments, and `.env` files are excluded from synchronization. Keep secrets outside the source tree.

Each job permits one active run and disables task retries and serverless automatic retry optimization. This reduces accidental reruns of the append-and-mark workflow. It does not coordinate manual notebooks or another deployment writing the same schema. Keep one publisher/import writer, choose fresh IDs and revisions, and inspect partial writes before starting a replacement run. Job-level parameters supply all notebook values; task-level `base_parameters` are intentionally absent.

The installed CLI v0.269.0 emitted a JSON schema against which the combined YAML passed an **offline** check; notebook paths were also resolved locally. Authentication-backed `bundle validate`, deployment, and job execution remain unrun. Configuration validation is not evidence of a successful cloud run.

## Publish the synthetic campaign revision

Set these notebook widgets:

- `repo_root`: the absolute Workspace path to the deployed repository root.
- `catalog`: your writable catalog, such as `demo_catalog`.
- `publication_id`: a fresh identifier such as `publication_cloud_01`; letters, numbers, and underscores only, starting with a letter/underscore. Blank generates a unique identifier.
- `business_revision`: an immutable business revision, such as `e_shop_campaigns_v1`; blank uses the publication ID.
- `export_volume`: an existing path such as `/Volumes/demo_catalog/festive_offer_e2e/artifacts`.
- `publish_contracts`: `true`.
- `operational_export_dir`: blank for this first run.

Run the notebook. The shared `prepare_bundle(raw, revision)` routine evaluates the original source rows individually. The current fixture contains eight contracts: seven are approved and `diwali_unknown_cap` is quarantined with `unknown_cap`. The approved batch yields 21 candidate decisions and four ranked rows. These counts were observed locally from the shared functions; they are expected cloud results to verify.

The notebook appends records to these Delta tables in `<catalog>.festive_offer_e2e`:

- `publication_sources`: canonical complete input and source hash for replay.
- `raw_contracts`: one original contract per source index.
- `approved_contracts`: versioned contract rows with the canonical compiled contract JSON.
- `quarantined_contracts`: rejected source rows and reasons.
- `publication_decisions`: one approved-contract decision per synthetic checkout, including ranked and refused candidates.
- `publication_metrics`: operational metrics for the approved batch.
- `publication_manifest`: revision, counts, content hash, complete bundle JSON, artifact path, and completion time.

It also writes three files under `<export_volume>/<publication_id>/`:

- `serving_bundle.json`: the application contract; it contains a manifest, approved offers, assumptions, and quarantined records. It contains no customer wallets or baskets.
- `source_dataset.json`: the retained full synthetic source, including client fixtures, for reproducible execution.
- `synthetic_client_inputs.json`: wallets and checkout fixtures kept separately from serving rules.

The final `publication_manifest` row marks a complete publication. A preceding table write, directory, or file alone is not sufficient. Tables and files do not share a transaction. Use one notebook writer, unique publication IDs, and fresh revisions. The guards protect sequential reruns, not competing writers. A failed run can leave partial evidence; inspect it and rerun under a fresh ID. Do not activate it in a client.

The manifest identifies schema/scoring versions, source/approved/quarantine counts, and a SHA-256 over canonical JSON `{offers, assumptions, quarantined}`. The exact `verify_bundle` routine is shared by the notebook and application. It detects partial or modified delivery; it is not a cryptographic signature authenticating an untrusted publisher. Protect the storage and download channel.

## Run the application against that exact bundle

After the notebook prints a completed publication, download `serving_bundle.json` and `source_dataset.json` through Catalog Explorer. With an already authenticated Databricks CLI, the equivalent commands from the local repository root are:

```bash
databricks fs cp dbfs:/Volumes/demo_catalog/festive_offer_e2e/artifacts/publication_cloud_01/serving_bundle.json ./downloaded_serving_bundle.json
databricks fs cp dbfs:/Volumes/demo_catalog/festive_offer_e2e/artifacts/publication_cloud_01/source_dataset.json ./downloaded_source_dataset.json
PYTHONPATH=src python3 -m festive_offers.online.system_demo --data downloaded_source_dataset.json --bundle downloaded_serving_bundle.json --output output --run-id cloud_roundtrip_01
```

Replace the catalog, publication ID, and paths with those printed by your notebook. The `system_demo` entry point loads the downloaded contract rather than independently publishing a second source snapshot. Its deterministic fixture clock exercises the festival windows even when run later. It uses the separate synthetic client inputs and local funding configuration; the bundle does not grant authority over an issuer's money.

The local run directory is `output/cloud_roundtrip_01/`. Its database files, scenario summary, and `exports/` capture the reference lifecycle. `exports/export_manifest.json` identifies a consistent SQLite snapshot of table exports. Its canonical JSON hashes are distinct from literal file-byte hashes; reports have the separately stated pre-export scope recorded in the manifest.

The local `batch/` compatibility output evaluates all eight original source rows, including the refusal for the incomplete contract, so it contains 24 decisions. The cloud publication's decision table evaluates only seven approved contracts and contains 21 decisions. Both produce the same four ranked rows. The online service uses the approved bundle throughout.

For a no-account run, use the local command described in [readme.md](../readme.md); without `--bundle`, the same publication routine prepares the synthetic fixture locally. That route requires no Databricks credentials.

## Bring the operational evidence back to Delta

Upload the contents of `output/cloud_roundtrip_01/exports/` to a fresh volume directory, such as `/Volumes/demo_catalog/festive_offer_e2e/artifacts/operations_cloud_01`. You can use Catalog Explorer or:

```bash
databricks fs cp output/cloud_roundtrip_01/exports dbfs:/Volumes/demo_catalog/festive_offer_e2e/artifacts/operations_cloud_01 --recursive
```

Confirm that `export_manifest.json` and the table JSON files are directly inside `operations_cloud_01`, rather than an extra nested `exports` directory.

Set `publish_contracts=false`, `operational_export_dir` to that directory, and `ingestion_id` to a fresh value such as `operations_import_01`. Keep `repo_root` and `catalog` configured, then run the notebook again.

With the bundle deployed, the corresponding command is:

```bash
databricks bundle run -t dev import_operations -- --operational_export_dir=/Volumes/demo_catalog/festive_offer_e2e/artifacts/operations_cloud_01 --ingestion_id=operations_import_01
```

An import-only run without `operational_export_dir` fails explicitly. It cannot report success after silently importing nothing.

The importer uses the application's actual `EXPORT_TABLES` list. It requires every table export, verifies each canonical JSON hash, and appends exact payloads to `operational_records`. It writes `operational_imports` last, retaining the export manifest, per-file byte hashes, row counts, and source path. A completion marker excludes partial imports from analytical queries. Imports are bounded to small demonstration files and records; this is a batch evidence transfer, not CDC or a live balance replica.

Run [`sql/inspect_end_to_end.sql`](../sql/inspect_end_to_end.sql) in a SQL warehouse with `catalog`, `publication_id`, and `ingestion_id` parameters matching the completed runs. Its queries show approval/quarantine counts, the expected-versus-nominal ranking, event counts, application states, budget conservation, and benefit obligations. A revision/hash comparison checks that the application's exported snapshot matches the published bundle. Run the publication queries after publication; operational tables exist after the optional import.

The conservation query checks `funding = available + held + committed` for each imported campaign. These balances describe that exported point in time. Admission must continue to use the native transactional ledger. A settled payment and a fulfilled cashback obligation remain different analytical states.

## Replay and recover

For contract replay, select the completed `publication_manifest` row by publication ID, extract `bundle_json`, and run the shared `verify_bundle`. Use the corresponding `publication_sources.source_json` and recorded scoring version to reproduce the synthetic batch. The revision and hash identify the business artifact; a table version is additional storage provenance.

For operational replay, retain the complete local run evidence: source, serving bundle, run/scenario manifests, batch decisions, and exported tables/reports with `export_manifest.json`. From the repository root, the portable observed result can be verified directly:

```bash
./record_results.sh --verify-run docs/results/evidence
```

For another completed local application run, pass its `output/<run_id>` directory. This command reads JSON, verifies hashes and publication identity, reconstructs funding from events, and compares it with native exported balances. It opens no SQLite database and sends no payment. [The results guide and snapshots](results/readme.md) show the observed output and explain the remaining cashback owed.

Reimporting the same operational exports under a new ingestion ID creates a separate analytical snapshot; queries must select one ID, rather than sum balances across snapshots. Incomplete imports receive no completion marker and are excluded by the supplied SQL joins.

Cleanup is explicit: remove only the demo publication/import artifacts and tables you no longer need. The notebook contains no automatic drop commands. Keep retained business revisions, financial evidence, and operational history according to the actual system's retention policy.

## Connect the application to Lakebase Autoscaling

[`src/festive_offers/online/lakebase.py`](../src/festive_offers/online/lakebase.py) supplies `make_connection_factory()`, which returns a callable opening one fresh psycopg connection. It targets **Lakebase Autoscaling** project/branch/endpoint resources. Provisioned instances use a different SDK API; instance names are rejected by this connector.

Install the optional dependencies in a virtual environment:

```bash
python3 -m pip install -r requirements-databricks.txt
```

Configure these connection coordinates through your deployment's environment or secret manager:

```text
ENDPOINT_NAME=projects/<project-id>/branches/<branch-id>/endpoints/<endpoint-id>
PGHOST=<host from the Lakebase connection dialog>
PGDATABASE=<existing database>
PGUSER=<authorized Postgres OAuth role>
PGPORT=5432
PGSSLMODE=require
PGCONNECT_TIMEOUT=10
```

For an external service, configure Databricks unified authentication separately, typically `DATABRICKS_HOST`, `DATABRICKS_CLIENT_ID`, and a secret-managed `DATABRICKS_CLIENT_SECRET`. The identity needs workspace access and a matching Postgres OAuth role with the required database/schema/table privileges. Follow the official [external application connection guide](https://docs.databricks.com/aws/en/oltp/projects/external-apps-connect) for identity and role setup. The factory does not create a project, endpoint, database, role, or synced table.

Each connection calls `WorkspaceClient().postgres.generate_database_credential(endpoint=...)` through the reused SDK client, then passes the returned token as psycopg's password. Lakebase database credentials expire; a token is never retained as a permanent password. Credential failure opens no database connection and does not fall back to `PGPASSWORD`. TLS is required; `verify-ca` and `verify-full` are supported, with `PGSSLROOTCERT` when your deployment requires an explicit trust root. `require` provides encrypted transport; configure certificate/hostname verification for the deployment's trust requirements.

The PostgreSQL store owns transaction setup and closes every returned connection. A new connection therefore receives a newly generated credential without adding a pool to this small reference. A service expecting high connection rates should add and verify a pool with bounded lifetime and token rotation rather than generating credentials for every request at scale. The seven local factory tests inject a fake SDK and connector to verify fresh credentials, refusal paths, and option handling. They make no claim about successful authentication or Lakebase availability.

The adapter is wired through the same store API as plain PostgreSQL:

```python
from festive_offers.online.lakebase import make_connection_factory
from festive_offers.online.postgres_store import PostgreSQLStore

store = PostgreSQLStore(
    connection_factory=make_connection_factory(),
    schema="festive_offers",
    initialize=False,
)
```

Initialize the native application tables once with `initialize=True` under an identity authorized to create the demo schema/tables, then run with the intended operational privileges. This does not initialize managed synced tables. Keep those rule projections separate from the native budget, usage, application, outbox, and evidence tables.

The bundle handles Lakehouse preparation/import jobs. The application runtime and its native database are separate deployable components; follow [the runtime instructions](runtime.md) for launching the application and selecting its database backend.

## Production deployment boundary

The code includes the publication/import notebook, two on-demand job definitions, the reference operational lifecycle, a PostgreSQL store, and the optional Lakebase connector. A deployment still needs its own database/identity setup, network authorization, trusted wallet/payment inputs, actual provider integration, monitoring, and recovery validation. The local provider remains synthetic. PostgreSQL tests and SQLite's local transaction behavior are not evidence of Lakebase failover, platform lock behavior, or production throughput.

If managed synced tables replace file transfer for approved rules, treat the projection as read-only and keep budget/usage/obligation writes in native Postgres tables. Verify complete revision readiness before activation; asynchronous synchronization does not authorize an application or guarantee a provider outcome. This notebook does not provision those resources.

## Documentation checked

Official sources accessed **6 November 2026**:

- [Unity Catalog volumes](https://docs.databricks.com/aws/en/volumes/volume-files), updated 27 October 2026: JSON artifacts, POSIX `/Volumes/...` access, upload/download options.
- [Volume privileges](https://docs.databricks.com/aws/en/volumes/privileges), updated 11 September 2026: parent-object and read/write permissions.
- [Databricks CLI filesystem commands](https://docs.databricks.com/aws/en/dev-tools/cli/reference/fs-commands), updated 11 September 2026: `dbfs:/Volumes/...` paths and file/directory copy.
- [Managed tables](https://docs.databricks.com/aws/en/tables/managed): Delta table storage and access.
- [Synced tables](https://docs.databricks.com/aws/en/oltp/projects/sync-tables), updated 5 November 2026: optional asynchronous serving bridge and native-table ownership boundary.

Additional implementation sources accessed **7 November 2026**:

- [Bundle configuration](https://docs.databricks.com/aws/en/dev-tools/bundles/settings) and [workspace file paths](https://docs.databricks.com/aws/en/dev-tools/bundles/reference): bundle layout and deployed source paths.
- [Job task configuration](https://docs.databricks.com/aws/en/dev-tools/bundles/job-task-types), updated 20 October 2026: workspace notebook tasks and disabling serverless automatic retry optimization.
- [Job parameters](https://docs.databricks.com/aws/en/dev-tools/bundles/job-parameters), updated 11 September 2026: runtime parameter overrides and job-level configuration.
- [Lakebase external applications using the SDK](https://docs.databricks.com/aws/en/oltp/projects/external-apps-connect), updated 30 October 2026: Autoscaling endpoint names, OAuth role setup, and fresh database credentials per connection.
- [Databricks SDK v0.89.0 source](https://github.com/databricks/databricks-sdk-py/blob/v0.89.0/databricks/sdk/service/postgres.py): the pinned SDK exposes `postgres.generate_database_credential(endpoint=...)`.
