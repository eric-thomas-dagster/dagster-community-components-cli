# Apache Iceberg → Dagster pipeline blueprint

Read from and write back to **existing Iceberg tables** owned by other engines — Snowflake, Trino, Spark, Flink, Databricks, etc. Engine-agnostic via PyIceberg.

## Why this exists

The official [`dagster_iceberg`](https://pypi.org/project/dagster-iceberg/) package only ships an IO manager — it makes Dagster the **owner** of the table. Most real lakehouse scenarios are different: **another engine owns the table**, and Dagster is one of several readers/writers in a cross-engine flow.

This walkthrough uses `iceberg_ingestion` (read) and `dataframe_to_iceberg_table` (write) to fill that gap.

## Architecture — typical lakehouse flow

```
   ┌─────────────────────────────────────────────────────┐
   │ Iceberg-compatible writer (any of these)            │
   │   • Snowflake (managed catalog → S3)                │
   │   • Trino / Starburst                               │
   │   • Spark / Databricks                              │
   │   • Flink                                           │
   └─────────────────────────────┬───────────────────────┘
                                 │ writes data files + commits
                                 ▼
   ┌─────────────────────────────────────────────────────┐
   │ Iceberg catalog (REST / Glue / Hive / Hadoop / SQL) │
   │   namespace.table → manifest list → data files      │
   └─────────────────────────────┬───────────────────────┘
                                 │
                                 ▼
   ┌─────────────────────────────────────────────────────┐
   │ iceberg_ingestion (asset)                           │
   │   table.scan(row_filter, selected_fields)           │
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
   │ dataframe_to_iceberg_table (sink)                   │
   │   table.append(arrow) OR overwrite(filter=...)      │
   └─────────────────────────────────────────────────────┘
```

## Components used

| Component | Source | Role |
|---|---|---|
| `iceberg_catalog_resource` | community | Register catalog config once (REST / Glue / Hive / Hadoop / SQL) |
| `iceberg_ingestion` | community | Read existing table → DataFrame |
| `dataframe_to_iceberg_table` | community | Append / overwrite an existing table |
| `external_iceberg_table` | community | Declare externally-owned table for lineage |
| `summarize`, `dataframe_join`, `sort`, `dataframe_to_*` | community | Standard transforms + alternate sinks |

## Catalog config matrix

### REST — Polaris (Apache), Nessie, Lakekeeper, Tabular, S3 Tables

```yaml
catalog_type: rest
catalog_properties:
  uri: https://catalog.example.com
  credential: "${CATALOG_CREDENTIAL}"   # OAuth: 'client_id:client_secret'
  warehouse: my_warehouse
  # token: "${BEARER_TOKEN}"            # alt: bearer
```

### REST — Snowflake-managed Iceberg catalog

```yaml
catalog_type: rest
catalog_properties:
  uri: https://abc-xy12345.snowflakecomputing.com/api/v2/catalogs/MY_CAT/iceberg
  credential: "${SNOWFLAKE_PAT}"
  warehouse: MY_WAREHOUSE
  scope: PRINCIPAL_ROLE:MY_ROLE
```

### REST — AWS S3 Tables (`s3tables`)

```yaml
catalog_type: rest
catalog_properties:
  uri: https://s3tables.us-east-1.amazonaws.com/iceberg
  warehouse: arn:aws:s3tables:us-east-1:123456789012:bucket/my-tables-bucket
  # AWS auth via env / instance profile / IRSA
```

### AWS Glue Data Catalog

```yaml
catalog_type: glue
catalog_properties:
  warehouse: s3://my-bucket/iceberg/
  # AWS_PROFILE / instance profile / IRSA for auth
```

### Hadoop catalog (filesystem-only)

```yaml
catalog_type: hadoop
catalog_properties:
  warehouse: s3://my-bucket/iceberg-warehouse/
```

## defs.yaml — Read existing Iceberg table

```yaml
# defs/orders_raw/defs.yaml
type: dagster_community_components.IcebergIngestionComponent
attributes:
  asset_name: orders_raw
  catalog_type: rest
  catalog_properties:
    uri: https://catalog.example.com
    credential: "${CATALOG_CREDENTIAL}"
    warehouse: my_warehouse
  namespace: sales
  table_name: orders
  select_columns: [order_id, customer_id, amount, order_date, status]
  row_filter: order_date >= '{partition_key}' and order_date < '{partition_key_next}'
  partition_type: daily
  partition_start: '2024-01-01'
  group_name: lakehouse
  kinds: [iceberg, lakehouse]
```

## defs.yaml — Transform + write back

```yaml
# defs/orders_by_region/defs.yaml — aggregate
type: dagster_community_components.SummarizeComponent
attributes:
  asset_name: orders_by_region
  upstream_asset_key: orders_raw
  group_by: [region]
  aggregations:
    total_revenue: {col: amount, agg: sum}
    n_orders: {col: order_id, agg: count}
```

```yaml
# defs/orders_by_region_loaded/defs.yaml — write to a different Iceberg table
type: dagster_community_components.DataframeToIcebergTableComponent
attributes:
  asset_name: orders_by_region_loaded
  upstream_asset_key: orders_by_region
  catalog_type: rest
  catalog_properties:
    uri: https://catalog.example.com
    credential: "${CATALOG_CREDENTIAL}"
    warehouse: my_warehouse
  namespace: analytics
  table_name: orders_by_region
  mode: overwrite
  overwrite_filter: business_date = '{partition_key}'
  partition_type: daily
  partition_start: '2024-01-01'
```

The `overwrite_filter` makes daily backfills idempotent — re-running a partition replaces just that day's rows in the analytics table.

## Resource pattern — register catalog once

For multiple components reading from the same catalog:

```yaml
# resources/lakehouse_catalog.yaml
type: dagster_community_components.IcebergCatalogResourceComponent
attributes:
  resource_key: lakehouse
  catalog_type: rest
  catalog_properties:
    uri: https://catalog.example.com
    credential: "${CATALOG_CREDENTIAL}"
    warehouse: my_warehouse
```

Custom assets can then:
```python
@asset(required_resource_keys={"lakehouse"})
def my_asset(context):
    table = context.resources.lakehouse.load_table("sales", "orders")
    return table.scan().to_arrow().to_pandas()
```

## External-asset declaration — for lineage clarity

When you want the upstream Iceberg table to appear in the asset graph (without materializing it through Dagster):

```yaml
# defs/orders_external/defs.yaml
type: dagster_community_components.ExternalIcebergTableAsset
attributes:
  asset_key: sales/orders
  catalog_name: lakehouse_main
  namespace: sales
  table_name: orders
  warehouse: s3://my-bucket/warehouse
  owner_engine: snowflake     # tags the asset with "snowflake" + "iceberg" + "lakehouse" kinds
  description: "Orders fact table — written by Snowflake's managed Iceberg catalog"
  group_name: lakehouse
```

Then in `orders_raw`'s defs, add `deps: [sales/orders]` so the lineage edge shows up.

## Time travel patterns

```yaml
# Pin to a specific snapshot (deterministic backfill)
snapshot_id: 12345

# Or: read snapshot active at a timestamp
as_of_timestamp_ms: 1716200000000

# Or: read from a named branch (Iceberg branching v2)
branch: dev
```

For backfills, prefer `snapshot_id` — it's stable across compactions. Timestamps shift after `expire_snapshots`.

## Partition predicate pushdown

Iceberg manifests track per-file column min/max. The `row_filter` is pushed down — only data files matching the filter are read:

```yaml
row_filter: order_date >= '{partition_key}' and order_date < '{partition_key_next}'
```

For a 100M-row table partitioned by `order_date`, this is the difference between scanning 100M rows and ~300k rows per daily partition.

## Storage credentials

Iceberg data files live on S3 / ADLS / GCS. PyIceberg uses fsspec — pick up credentials from env:

```bash
# S3
export AWS_REGION=us-east-1
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
# Or use instance profile / IRSA / aws sso login

# ADLS Gen2
export AZURE_STORAGE_ACCOUNT_NAME=myaccount
export AZURE_STORAGE_KEY=...
# Or DefaultAzureCredential with Workload Identity

# GCS
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa-key.json
# Or workload identity
```

## Dependencies

```
pyiceberg>=0.6.0
pyarrow>=10.0.0
pandas>=1.5.0
```

Plus extras per catalog/storage:

```bash
pip install 'pyiceberg[s3fs,glue]'   # most common AWS combo
pip install 'pyiceberg[adlfs]'        # ADLS
pip install 'pyiceberg[gcs]'          # GCS
pip install 'pyiceberg[hive]'         # Hive Metastore
pip install 'pyiceberg[sql-postgres]' # Postgres-backed SQL catalog
```

No JVM. No Spark. Single Python wheel.

## Trade-offs & gotchas

- **PyIceberg's MERGE/upsert is limited.** Use Spark / Trino / Snowflake for complex MERGE logic. Append + partition-overwrite handles 90% of batch use cases.
- **Schema evolution.** If the writer adds columns, `iceberg_ingestion` picks them up automatically. `dataframe_to_iceberg_table` requires `schema_evolution: true` for new columns on writes (default off, safer).
- **Concurrent writers.** Iceberg's optimistic concurrency rebases on conflict — typically transparent, but very-high-write-rate workloads should serialize via a single writer.
- **Storage-IAM mismatch.** The catalog says "your table is at s3://...", but if your runtime can't reach S3, reads fail with confusing errors. Validate storage credentials separately first.

## See also

- [`delta_pipeline.md`](delta_pipeline.md) — sister walkthrough for Delta Lake
- [`snowflake_iceberg_databricks.md`](snowflake_iceberg_databricks.md) — full cross-vendor: Snowflake writes Iceberg → Databricks Lakeflow reads via Unity Catalog
- [`iceberg_io_manager` walkthrough](https://dagster-community-components-cli.vercel.app/c/iceberg_io_manager) — when Dagster OWNS the table
- [Apache Iceberg docs](https://iceberg.apache.org/)
- [PyIceberg docs](https://py.iceberg.apache.org/)
