# Lakehouse local roundtrip — Iceberg + Delta without cloud

A complete `dataframe → table → dataframe` cycle for both **Apache Iceberg** and **Delta Lake**, running entirely against the local filesystem. No S3, no Glue, no Snowflake catalog, no Spark/JVM, no auth. Validates that the Iceberg and Delta family of community components work end-to-end before you point them at a real lakehouse.

## Components used

| Component | Source | Role |
|---|---|---|
| `synthetic_data_generator` | community | Seeds pandas DataFrames (`orders`, `events`) — upstream of the writers |
| `iceberg_catalog_resource` | community | Shared catalog config (here: SQLite-backed local catalog) |
| `dataframe_to_iceberg_table` | community | Writes a DataFrame to an Iceberg table |
| `iceberg_ingestion` | community | Reads an Iceberg table back into a DataFrame |
| `dataframe_to_delta_table` | community | Writes a DataFrame to a Delta table |
| `delta_ingestion` | community | Reads a Delta table back into a DataFrame |
| `external_iceberg_table` | community | Declare-only AssetSpec for the same Iceberg table |
| `external_delta_table` | community | Declare-only AssetSpec for the same Delta path |

## Architecture

```
   ┌─────────────────────┐    ┌─────────────────────┐
   │ synthetic_orders    │    │ synthetic_events    │
   │ (generator → df)    │    │ (generator → df)    │
   └──────────┬──────────┘    └──────────┬──────────┘
              │                          │
              ▼                          ▼
   ┌─────────────────────┐    ┌─────────────────────┐
   │ orders_to_iceberg   │    │ events_to_delta     │
   │ pyiceberg write     │    │ deltalake write     │
   │ → /tmp/iceberg-     │    │ → /tmp/delta-events │
   │   warehouse         │    │                     │
   └──────────┬──────────┘    └──────────┬──────────┘
              │                          │
              ▼                          ▼
   ┌─────────────────────┐    ┌─────────────────────┐
   │ orders_from_iceberg │    │ events_from_delta   │
   │ pyiceberg read      │    │ deltalake read      │
   └─────────────────────┘    └─────────────────────┘

   external/iceberg_orders   external/delta_events
   (declare-only AssetSpec)  (declare-only AssetSpec)
```

## Run it

```bash
bash setup_lakehouse_local_demo.sh
cd lakehouse-local-demo

uv run dg check defs
uv run dg list defs
uv run dg launch --assets '*'
```

Expect `RUN_SUCCESS` with 6 materialized assets + 2 declare-only externals.

## Iceberg catalog: `sql` not `hadoop`

`pyiceberg` doesn't ship a `hadoop` catalog (that's the JVM-era one). The modern equivalents are:

| `catalog_type` | What it is | When to use |
|---|---|---|
| `sql` | SQLite/Postgres/MySQL-backed catalog | Local dev (this demo); small self-hosted |
| `rest` | REST catalog API (Polaris/Nessie/Lakekeeper/Snowflake/S3 Tables) | Production multi-engine |
| `glue` | AWS Glue Data Catalog | AWS-native |
| `hive` | Hive Metastore | Existing Hadoop estates |

For this demo:

```yaml
catalog_type: sql
catalog_properties:
  uri: sqlite:////tmp/iceberg-warehouse/catalog.db
  warehouse: file:///tmp/iceberg-warehouse
```

A single SQLite file is the catalog. Parquet data files live under `warehouse/`. Same component, no JVM.

## Delta: no catalog at all

`deltalake` (the Rust impl) reads/writes Delta tables directly from a path or URI. No catalog server, no metastore:

```yaml
table_uri: /tmp/delta-events
mode: overwrite
```

Delta's transaction log (`_delta_log/`) lives next to the parquet files. To retarget at production, swap the path:

```yaml
table_uri: s3://my-bucket/lakehouse/events
storage_options:
  AWS_REGION: us-east-1
# or:
# table_uri: az://container@account.dfs.core.windows.net/lakehouse/events
# table_uri: gs://my-bucket/lakehouse/events
# table_uri: uc://main.sales.events   # Databricks Unity Catalog
```

## What the demo proves

- `synthetic_data_generator` produces typed pandas DataFrames that downstream sinks consume directly.
- `dataframe_to_iceberg_table` creates the table on first write (no manual `CREATE TABLE`), then `overwrite`s on subsequent runs.
- `dataframe_to_delta_table` does the same on the Delta side.
- `iceberg_ingestion` + `delta_ingestion` read the same tables back into DataFrames — proves the catalog metadata + data files round-trip cleanly.
- `iceberg_catalog_resource` can be used standalone (e.g., by a custom asset that wants `context.resources.iceberg`) or left out when each component embeds its own catalog config.
- `external_iceberg_table` + `external_delta_table` register the same physical tables as declare-only assets — right for when another engine (Snowflake / Spark / Databricks) is the writer and Dagster only needs to track lineage downstream.

## Trade-offs

- **SQLite catalog is single-writer.** Fine for dev / single-machine. For multi-process or concurrent writes, switch to Postgres-backed `sql` catalog or use `rest`.
- **pyiceberg's `merge`/upsert is limited.** This demo uses `mode: overwrite`. For real upserts, write the table from Spark/Trino and let Dagster orchestrate via `external_iceberg_table` instead.
- **Delta `mode: overwrite` replaces the entire table.** For partition-level overwrite use `overwrite_partition_filter`. For upserts use `mode: merge` with a `merge_predicate`.
- **Schema evolution.** Both `dataframe_to_iceberg_table` and `dataframe_to_delta_table` use the upstream DataFrame's schema. Reorder/rename upstream columns → table schema follows.

## See also

- [`iceberg_pipeline.md`](iceberg_pipeline.md) — protocol walkthrough (Snowflake/Polaris/Glue auth)
- [`delta_pipeline.md`](delta_pipeline.md) — protocol walkthrough (S3/ADLS/UC auth)
- [`snowflake_iceberg_databricks.md`](snowflake_iceberg_databricks.md) — cross-engine Iceberg with three different writers
- [`external_assets.md`](external_assets.md) — declare-only asset family
