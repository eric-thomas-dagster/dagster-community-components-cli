# Delta Lake → Dagster pipeline blueprint

Read from and write back to **existing Delta Lake tables** owned by other engines — Spark, Databricks, Trino, Flink — using `delta-rs` (no Spark / no JVM).

## Why this exists

The official [`dagster_deltalake`](https://pypi.org/project/dagster-deltalake/) package only ships IO managers and a resource — they make Dagster the **owner** of the table. Cross-engine scenarios where another engine owns the table need different components.

This walkthrough uses [`delta_ingestion`](https://dagster-component-ui.vercel.app/c/delta_ingestion) and [`dataframe_to_delta_table`](https://dagster-component-ui.vercel.app/c/dataframe_to_delta_table) — both backed by `delta-rs` (the Rust implementation of the Delta protocol).

## Architecture

```
   ┌─────────────────────────────────────────────────────┐
   │ Delta writer (any of these)                         │
   │   • Spark / Databricks (Unity Catalog or HMS)       │
   │   • Trino / Flink                                   │
   │   • delta-rs from another service                   │
   └─────────────────────────────┬───────────────────────┘
                                 │ writes Parquet + _delta_log
                                 ▼
   ┌─────────────────────────────────────────────────────┐
   │ Object storage / UC                                 │
   │   s3://.../path  |  abfss://.../path  |  uc://...   │
   └─────────────────────────────┬───────────────────────┘
                                 │
                                 ▼
   ┌─────────────────────────────────────────────────────┐
   │ delta_ingestion (asset) — uses delta-rs             │
   │   DeltaTable(...).to_pandas(columns, partitions)    │
   │   → pandas DataFrame                                │
   └─────────────────────────────┬───────────────────────┘
                                 │
                                 ▼
   ┌─────────────────────────────────────────────────────┐
   │ summarize / sort / filter / join / etc.             │
   └─────────────────────────────┬───────────────────────┘
                                 │
                                 ▼
   ┌─────────────────────────────────────────────────────┐
   │ dataframe_to_delta_table (sink)                     │
   │   append / overwrite / merge to existing table      │
   └─────────────────────────────────────────────────────┘
```

## Components used

| Component | Source | Role |
|---|---|---|
| [`delta_ingestion`](https://dagster-component-ui.vercel.app/c/delta_ingestion) | community | Read existing Delta table → DataFrame |
| [`dataframe_to_delta_table`](https://dagster-component-ui.vercel.app/c/dataframe_to_delta_table) | community | Append / overwrite / merge |
| [`external_delta_table`](https://dagster-component-ui.vercel.app/c/external_delta_table) | community | Declare externally-owned table for lineage |

## Storage matrix

| Backend | URI scheme | Storage options |
|---|---|---|
| **S3** | `s3://bucket/path` | `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_ENDPOINT_URL` (Minio/R2) |
| **ADLS Gen2** | `az://container@account.dfs.core.windows.net/path` | `AZURE_STORAGE_ACCOUNT_NAME`, `AZURE_STORAGE_KEY` |
| **GCS** | `gs://bucket/path` | `GOOGLE_SERVICE_ACCOUNT_KEY` |
| **Unity Catalog** | `uc://catalog.schema.table` | `UC_TOKEN`, `UC_HOST` |
| **Local** | `/local/path` | — |

`${ENV_VAR}` expansion supported in `storage_options`.

## defs.yaml — Read existing Delta table from S3

```yaml
# defs/events_raw/defs.yaml
type: dagster_community_components.DeltaIngestionComponent
attributes:
  asset_name: events_raw
  table_uri: s3://my-bucket/lakehouse/events
  storage_options:
    AWS_REGION: us-east-1
    # AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY from env / instance profile
  select_columns: [event_id, user_id, event_type, event_timestamp, properties]
  partition_filters:
    - [event_date, "=", "{partition_key}"]
  partition_type: daily
  partition_start: '2024-01-01'
  group_name: lakehouse
  kinds: [delta, lakehouse]
```

## defs.yaml — Read from Unity Catalog

For Databricks Unity Catalog tables, skip the raw storage path:

```yaml
attributes:
  asset_name: orders_uc
  table_uri: uc://main.sales.orders
  storage_options:
    UC_TOKEN: "${DATABRICKS_TOKEN}"
    UC_HOST: "https://my-workspace.cloud.databricks.com"
```

UC resolves the actual storage location automatically.

## defs.yaml — Append / overwrite / merge

```yaml
# Append — incremental ingestion
type: dagster_community_components.DataframeToDeltaTableComponent
attributes:
  asset_name: events_processed_loaded
  upstream_asset_key: events_processed
  table_uri: s3://my-bucket/lakehouse/events_processed
  storage_options:
    AWS_REGION: us-east-1
  mode: append
  partition_by: [event_date]
```

```yaml
# Partition-level overwrite — idempotent daily backfill
attributes:
  mode: overwrite
  overwrite_partition_filter:
    - [event_date, "=", "{partition_key}"]
  partition_type: daily
  partition_start: '2024-01-01'
```

```yaml
# MERGE (basic upsert) — match on key, update existing, insert new
attributes:
  mode: merge
  merge_predicate: "target.event_id = source.event_id"
```

For complex MERGE (selective updates, deletes, conditional inserts), drop down to a custom asset using `dt.merge(...)` from `deltalake` directly. The component handles the common case (match-update-all + unmatched-insert-all).

## External-asset declaration — for lineage

When Spark or Databricks writes the upstream table and you want it visible in Dagster's asset graph:

```yaml
# defs/events_external/defs.yaml
type: dagster_community_components.ExternalDeltaTableAsset
attributes:
  asset_key: lakehouse/events
  table_uri: s3://my-bucket/lakehouse/events
  owner_engine: spark
  description: "Events table written by Spark Structured Streaming"
  group_name: lakehouse
  tags:
    tier: bronze
```

Then in `events_raw`'s defs, add `deps: [lakehouse/events]` so the lineage edge appears.

## Time travel

```yaml
# Pin to a specific Delta version (deterministic — best for backfills)
version: 42

# Or: version active at a timestamp (best-effort)
timestamp: '2024-01-15T00:00:00Z'
```

For backfills, prefer `version`. Delta's VACUUM can compact away the files referenced by old timestamps if retention is short — versions are stable references; timestamps less so.

## Partition predicate pushdown

`delta-rs` filters at the file level using the manifest's partition stats:

```yaml
partition_filters:
  - [event_date, "=", "{partition_key}"]
  - [region, "in", "us-east-1,us-west-2"]
```

Supported ops: `=`, `!=`, `>`, `>=`, `<`, `<=`, `in`, `not in`.

## Why delta-rs (not pyspark)

| | delta-rs (this component) | pyspark |
|---|---|---|
| Install size | ~30 MB wheel | ~250 MB + JVM |
| Cold start | Instant | 5–15s (Spark session) |
| Concurrency | Native async-friendly | Heavyweight |
| MERGE support | Basic (match-all / unmatched-all) | Full Spark SQL MERGE |
| Schema evolution | Yes (`schema_mode: merge`) | Yes |
| When | k8s pods, serverless, Dagster Daemon | Big-data ETL with full Spark SQL |

For Dagster's batch pipeline pattern (read N→transform→write 1), delta-rs is the right shape.

## Comparison to siblings in the registry

| | This pair (`delta_ingestion` / `dataframe_to_delta_table`) | [`dataframe_to_databricks`](https://dagster-community-components-cli.vercel.app/c/dataframe_to_databricks) | [`delta_lake_io_manager`](https://dagster-community-components-cli.vercel.app/c/delta_lake_io_manager) |
|---|---|---|---|
| Where it lives | Anywhere Delta lives | Specifically Databricks SQL warehouse | Local / S3 / ADLS, Dagster-owned |
| Engine | delta-rs | Databricks SQL connector | delta-rs via official IO manager |
| Use when | Cross-engine Delta on S3 / ADLS / GCS / UC | Targeting Databricks SQL warehouse | Dagster owns the table |

## Trade-offs & gotchas

- **Concurrent writers.** Delta uses optimistic concurrency; conflicting writes retry-then-fail. For high write rates from multiple engines, prefer one source-of-truth writer.
- **VACUUM lag.** If you backfill old partitions and another writer just ran VACUUM with short retention, your version-pinned reads fail (data files gone). Set Delta's `delta.deletedFileRetentionDuration` to cover your backfill horizon.
- **Schema enforcement is strict by default.** Adding a new column from the DataFrame trips the writer unless `schema_mode: merge`. Cautious default — flip on explicitly when intentional.
- **`uc://` URIs need a UC-enabled Databricks workspace.** Hive Metastore tables don't expose this scheme; use the raw `s3://` / `abfss://` path.

## See also

- [`iceberg_pipeline.md`](iceberg_pipeline.md) — sister walkthrough for Apache Iceberg
- [`databricks_delta_to_dagster.md`](databricks_delta_to_dagster.md) — focused walkthrough: Databricks-written Delta → Dagster downstream
- [`dataframe_to_databricks` walkthrough](https://dagster-community-components-cli.vercel.app/c/dataframe_to_databricks) — alternate sink targeting Databricks SQL warehouse
- [Delta Lake docs](https://docs.delta.io/)
- [delta-rs (Python)](https://github.com/delta-io/delta-rs)
