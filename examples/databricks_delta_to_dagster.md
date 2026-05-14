# Databricks Delta → Dagster

A focused walkthrough for one common cross-vendor scenario: **Databricks writes Delta tables to Unity Catalog or to raw S3/ADLS; Dagster reads them downstream** for orchestration, ML, or BI extracts.

This is a Databricks-flavored take on [`delta_pipeline.md`](delta_pipeline.md). Same components — different config knobs.

## When to use each path

| | This walkthrough (`delta_ingestion`) | `dataframe_to_databricks` for writes | Official [`DatabricksWorkspaceComponent`](https://docs.dagster.io/integrations/databricks) for jobs |
|---|---|---|---|
| Direction | Read from Delta | Write to Databricks SQL warehouse | Trigger Databricks Jobs (Notebooks / pipelines) |
| Engine | delta-rs (no Spark) | Databricks SQL connector | Databricks Jobs API |
| When | Read-heavy, ML training, BI extracts | Push a DataFrame to Databricks | Wrap Lakeflow / Workflows pipelines in Dagster's asset graph |

## Architecture — UC-managed Delta (recommended)

```
   ┌─────────────────────────────────────────────────────┐
   │ Databricks (write side)                             │
   │   CREATE TABLE main.sales.orders ...                │
   │   USING DELTA                                       │
   │   LOCATION 's3://my-bucket/uc-managed/orders'       │
   └─────────────────────────────┬───────────────────────┘
                                 │ Lakeflow / Spark commits Delta versions
                                 ▼
   ┌─────────────────────────────────────────────────────┐
   │ Unity Catalog                                       │
   │   main.sales.orders → resolves to S3/ADLS path      │
   └─────────────────────────────┬───────────────────────┘
                                 │
                                 ▼
   ┌─────────────────────────────────────────────────────┐
   │ delta_ingestion (asset) — delta-rs                  │
   │   table_uri: uc://main.sales.orders                 │
   │   → pandas DataFrame                                │
   └─────────────────────────────────────────────────────┘
```

## Step 1 (one-time) — mint a Databricks PAT

1. In Databricks: `User Settings → Developer → Access Tokens → Generate New Token`
2. Set name + lifetime, copy the token
3. `export DATABRICKS_TOKEN=<token>`

The user needs `USE CATALOG / USE SCHEMA / SELECT` on the target table — typically via a Service Principal.

For production, prefer **OAuth M2M tokens via Service Principals**: more granular, rotatable, audit-friendly. Set up at `Account Console → Identity → Service Principals → OAuth secrets`. Then use `oauth_token_resource` with `grant_type: client_credentials`.

## Step 2 — read from Dagster (UC scheme)

```yaml
# defs/orders_delta/defs.yaml
type: dagster_community_components.DeltaIngestionComponent
attributes:
  asset_name: orders_delta
  table_uri: uc://main.sales.orders        # uc:// scheme — UC resolves storage
  storage_options:
    UC_TOKEN: "${DATABRICKS_TOKEN}"
    UC_HOST: "https://my-workspace.cloud.databricks.com"
  select_columns: [order_id, customer_id, amount, order_date, region]
  partition_filters:
    - [order_date, "=", "{partition_key}"]
  partition_type: daily
  partition_start: '2024-01-01'
  group_name: databricks
  kinds: [delta, databricks, lakehouse]
```

## Or: read raw S3/ADLS path (HMS / non-UC)

If you're on a Databricks workspace without Unity Catalog (Hive Metastore only), use the raw path:

```yaml
attributes:
  asset_name: orders_delta
  table_uri: s3://my-bucket/hms-managed/orders
  storage_options:
    AWS_REGION: us-east-1
    # AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY from env / IRSA
```

Or ADLS:

```yaml
attributes:
  table_uri: az://my-container@my-account.dfs.core.windows.net/orders
  storage_options:
    AZURE_STORAGE_ACCOUNT_NAME: my-account
    AZURE_STORAGE_KEY: "${AZURE_STORAGE_KEY}"
```

## Step 3 — declare the upstream for lineage

```yaml
# defs/orders_external/defs.yaml
type: dagster_community_components.ExternalDeltaTableAsset
attributes:
  asset_key: databricks/orders
  table_uri: uc://main.sales.orders
  owner_engine: databricks
  description: "Orders Delta table maintained by Databricks Lakeflow pipeline"
  group_name: databricks
  partition_type: daily
  partition_start: '2024-01-01'
```

Then in `orders_delta`'s defs, add `deps: [databricks/orders]`.

> **Already have `external_databricks_table`?** Yes — that component is Databricks-specific (workspace URL + catalog + schema fields). `external_delta_table` is engine-agnostic with `owner_engine: databricks`. Use whichever fits your conventions; they don't conflict.

## Time travel — re-runnable backfills

```yaml
# Pin to a specific Delta version (deterministic — best for backfills)
version: 42

# Or: timestamp (best-effort — VACUUM can erase data files)
timestamp: '2024-01-15T00:00:00Z'
```

For Dagster partitions backfilling over weeks, use `version`. Coordinate with the Databricks team on `delta.deletedFileRetentionDuration` so old data files aren't VACUUMed before your backfill runs.

## Writing back to Databricks Delta

Two paths:

### Path A — delta-rs (raw S3/ADLS, cross-engine safe)

```yaml
type: dagster_community_components.DataframeToDeltaTableComponent
attributes:
  asset_name: order_summary_loaded
  upstream_asset_key: order_summary
  table_uri: s3://my-bucket/uc-managed/order_summary
  storage_options: {AWS_REGION: us-east-1}
  mode: overwrite
  overwrite_partition_filter:
    - [business_date, "=", "{partition_key}"]
  partition_by: [business_date]
```

Works for any Delta destination. Databricks Jobs / Lakeflow can read it immediately — Delta is just files.

### Path B — `dataframe_to_databricks` (Databricks SQL warehouse)

```yaml
type: dagster_community_components.DataframeToDatabricksComponent
attributes:
  # ... see component docs for full config
```

Routes through Databricks SQL warehouse. Costs Databricks DBU credits per write but lets Databricks do server-side compaction / cluster management.

## Triggering Databricks pipelines / jobs

For the **trigger-Databricks-from-Dagster** direction (rather than read-the-output), use the official [`DatabricksWorkspaceComponent`](https://docs.dagster.io/integrations/databricks) — it wraps Databricks Jobs API. Pair it with `delta_ingestion` for the read side and you get the full lineage: Dagster triggers the Databricks Job → Job writes Delta → Dagster reads Delta downstream.

This is the pattern in [`snowflake_iceberg_databricks.md`](snowflake_iceberg_databricks.md).

## Trade-offs & gotchas

- **UC-resolved storage credentials.** The `uc://` scheme has UC mint short-lived storage tokens automatically — much simpler than managing AWS / Azure credentials per table.
- **Network access.** `uc://` requires the Dagster runtime to reach the Databricks workspace URL + the underlying storage. Raw `s3://` only needs S3 access.
- **PAT expiration.** Build alerts before token expiry. OAuth M2M is the better long-term path.
- **delta-rs vs pyspark compatibility.** Almost-but-not-quite. Some Databricks-specific Delta extensions (e.g. Liquid Clustering metadata, identity columns) don't round-trip perfectly through delta-rs. Stick to vanilla Delta features for cross-engine tables.
- **VACUUM and time-travel.** Databricks default VACUUM retention is 7 days. Backfills older than that fail. Set `delta.deletedFileRetentionDuration` to cover your backfill horizon.

## See also

- [`delta_pipeline.md`](delta_pipeline.md) — generic Delta walkthrough
- [`snowflake_to_dagster_iceberg.md`](snowflake_to_dagster_iceberg.md) — sister scenario: Snowflake → Dagster
- [`snowflake_iceberg_databricks.md`](snowflake_iceberg_databricks.md) — full cross-vendor blueprint with Databricks Lakeflow
- [Databricks Delta docs](https://docs.databricks.com/en/delta/index.html)
- [Unity Catalog docs](https://docs.databricks.com/en/data-governance/unity-catalog/)
