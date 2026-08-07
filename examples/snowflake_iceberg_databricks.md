# Snowflake → Iceberg → Databricks Lakeflow — blueprint
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

A common production pattern: **Snowflake** does the heavy SQL transformations on raw data, lands the result as an **Apache Iceberg** table in cloud storage (S3 / ADLS / GCS), and **Databricks Lakeflow Declarative Pipelines** (formerly DLT) read the same Iceberg table directly to feed downstream models / dashboards / lakehouse silver-gold layers.

Iceberg is an open table format — the table's metadata + data files in object storage are self-describing. Databricks Unity Catalog can register the same Iceberg table by its storage location, so the two engines share data without an intermediate REST-catalog server. Dagster orchestrates both sides and surfaces the cross-engine lineage in one catalog.

> **Validation status:** the Dagster wiring is buildable and passes `dg check` once env vars are set. The end-to-end pipeline has **not** been validated in this repo — it requires real Snowflake + Databricks accounts. This is a common, production-shape pattern, presented as a blueprint to copy into your environment.

## Architecture

```
   ┌────────────────────────────────────────┐
   │  SNOWFLAKE                              │
   │                                         │
   │   RAW.ORDERS ─┐                         │
   │              ├─► DYNAMIC ICEBERG TABLE  │
   │   RAW.CUSTS ─┘   SILVER.CUSTOMER_METRICS│
   │                  writes Iceberg files    │
   │                  to s3://bucket/.../     │
   └──────────────────────┬──────────────────┘
                          │
                ┌─────────▼─────────┐
                │ S3 / ADLS / GCS   │
                │ Iceberg metadata  │
                │ + data files       │
                └─────────┬─────────┘
                          │
   ┌──────────────────────▼──────────────────────────┐
   │  DATABRICKS                                      │
   │                                                  │
   │  Job: customer_metrics_enrichment                │
   │    └─ task_type: pipeline_task                   │
   │        └─ Lakeflow Declarative Pipeline:         │
   │             [REFRESH FOREIGN TABLE silver...]    │
   │             CREATE OR REFRESH STREAMING TABLE    │
   │               customer_metrics_enriched AS       │
   │               SELECT ... FROM silver.customer_   │
   │               metrics LEFT JOIN dim.customers    │
   └──────────────────────┬───────────────────────────┘
                          │
   ┌──────────────────────▼───────────────────────────┐
   │  DAGSTER                                          │
   │                                                   │
   │   snowflake_workspace                             │
   │     → snowflake_silver/CUSTOMER_METRICS  ────┐    │
   │                                              │    │
   │   DatabricksWorkspaceComponent (official)    │    │
   │     → databricks/lakeflow/                   │    │
   │       customer_metrics_enriched  ◄───deps────┘    │
   │                                                   │
   │   cron_schedule: silver_to_gold_daily @ 06:00 UTC │
   └───────────────────────────────────────────────────┘
```

## Components used

| Component | Source | Role |
|---|---|---|
| `snowflake_workspace` | **community** (no official equivalent) | Imports Snowflake Dynamic Tables / Tasks / Streams / Snowpipes as Dagster assets. The Dynamic Iceberg Table becomes a materializable asset; a sensor observes refreshes. Sits on top of `dagster-snowflake`'s `SnowflakeResource`. |
| `DatabricksWorkspaceComponent` | **official** (`dagster-databricks`) | Imports Databricks Jobs as Dagster assets (each job's tasks become assets). Lakeflow pipelines are surfaced by wrapping them in a Job with a `pipeline_task` — the standard Databricks pattern, which is how this customer already has them defined. |
| `cron_schedule` | community | Daily 06:00 UTC schedule that materializes the chain end-to-end in topological order. |

That's it — two assets in a deps chain (`snowflake_silver/CUSTOMER_METRICS` → `databricks/lakeflow/customer_metrics_enriched`), a schedule, and the two auto-generated sensors. Dagster sequences the materialization, the Lakeflow pipeline reads the Iceberg table when it runs.

## Prerequisites (you provide these)

### Snowflake side

1. **Account**: any Snowflake edition that supports Iceberg tables (Enterprise+).
2. **External volume** pointing at S3 (or Azure / GCS):
   ```sql
   CREATE OR REPLACE EXTERNAL VOLUME my_s3_iceberg_volume
     STORAGE_LOCATIONS = ((
       NAME = 'my_s3_us_east_1',
       STORAGE_PROVIDER = 'S3',
       STORAGE_BASE_URL = 's3://my-iceberg-bucket/',
       STORAGE_AWS_ROLE_ARN = 'arn:aws:iam::123456789012:role/SnowflakeIcebergRole',
       STORAGE_AWS_EXTERNAL_ID = 'snowflake_external_id'
     ));
   ```

3. **The Dynamic Iceberg Table** — Snowflake-managed, writes directly to S3:
   ```sql
   USE DATABASE ANALYTICS;
   USE SCHEMA SILVER;

   CREATE OR REPLACE DYNAMIC ICEBERG TABLE CUSTOMER_METRICS
     EXTERNAL_VOLUME = 'my_s3_iceberg_volume'
     CATALOG = 'SNOWFLAKE'                       -- Snowflake-managed catalog
     BASE_LOCATION = 'analytics/customer_metrics/'
     TARGET_LAG = '1 hour'
     WAREHOUSE = COMPUTE_WH
   AS
     SELECT
       customer_id,
       COUNT(DISTINCT order_id) AS lifetime_orders,
       SUM(total)               AS lifetime_revenue,
       MAX(order_date)          AS last_order_date
     FROM RAW.PUBLIC.ORDERS
     GROUP BY customer_id;
   ```

   The data + metadata files land at `s3://my-iceberg-bucket/analytics/customer_metrics/`. Snowflake writes the standard Iceberg metadata (`metadata.json`, manifest lists, data files in Parquet) that any Iceberg reader can consume.

   You can find the active metadata file location any time with:
   ```sql
   SELECT SYSTEM$GET_ICEBERG_TABLE_INFORMATION('ANALYTICS.SILVER.CUSTOMER_METRICS');
   ```

### Databricks side

1. **Unity Catalog** workspace with metastore.

2. **External Iceberg table** in UC pointing at the same S3 location — no REST-catalog federation required, just the storage path:
   ```sql
   -- In a Databricks SQL editor or notebook
   CREATE TABLE main.silver.customer_metrics
     USING ICEBERG
     LOCATION 's3://my-iceberg-bucket/analytics/customer_metrics/';
   ```

   Databricks reads the Iceberg metadata files from S3 to discover the latest snapshot. After Snowflake refreshes the Dynamic Table, run `REFRESH TABLE main.silver.customer_metrics` (or let Lakeflow's incremental refresh handle it) so Databricks picks up the new metadata.

3. **Lakeflow Declarative Pipeline** named `customer_metrics_enrichment` defined as (illustrative SQL):
   ```sql
   CREATE OR REFRESH STREAMING TABLE customer_metrics_enriched
   AS SELECT
     m.customer_id,
     m.lifetime_orders,
     m.lifetime_revenue,
     CASE WHEN m.lifetime_revenue > 1000 THEN 'high' ELSE 'low' END AS tier,
     d.region,
     d.signup_date
   FROM main.silver.customer_metrics m            -- ← UC external Iceberg
   LEFT JOIN main.dim.customers d USING (customer_id);
   ```

4. **A Job that wraps the Lakeflow pipeline.** The official `DatabricksWorkspaceComponent` imports **Jobs**, not raw Lakeflow pipelines — so create a Job in Databricks (UI: Workflows → New Job → Task type "Pipeline" → pick `customer_metrics_enrichment`). Note the **Job ID** (visible in the URL or Job details panel). You'll plug it into `DATABRICKS_LAKEFLOW_JOB_ID`.

5. **A SQL Warehouse** for the `REFRESH TABLE` glue step. Create a serverless or classic SQL Warehouse, note its **HTTP path** (`/sql/1.0/warehouses/<id>`).

6. **A personal access token** with permissions to read/run jobs + use the SQL warehouse.

## Setup (Dagster side)

```bash
./setup_snowflake_iceberg_databricks_demo.sh
cd snowflake-iceberg-databricks-demo

# Env vars (paste your real values)
export SNOWFLAKE_ACCOUNT=myorg-us-east-1
export SNOWFLAKE_USER=dagster_user
export SNOWFLAKE_PASSWORD=...
export DATABRICKS_HOST=https://dbc-xxx.cloud.databricks.com
export DATABRICKS_TOKEN=dapi...
export DATABRICKS_LAKEFLOW_JOB_ID=12345                           # the Job wrapping the Lakeflow pipeline

# Validate the YAML loads + components resolve
uv run dg check defs

# Launch the UI
uv run dg dev
# → http://localhost:3000
```

## What you'll see in the catalog

Two assets in a deps chain, one schedule, two sensors, one freshness policy:

| Asset | Type | Materialization |
|---|---|---|
| `snowflake_silver/CUSTOMER_METRICS` | Snowflake Dynamic Iceberg Table | Triggered by Snowflake's `TARGET_LAG = '1 hour'` or by the daily schedule; sensor observes external refreshes |
| `databricks/lakeflow/customer_metrics_enriched` | Databricks Job → Lakeflow pipeline (official `DatabricksWorkspaceComponent`) | `deps: [snowflake_silver/CUSTOMER_METRICS]`. Materializing triggers the Databricks Job that runs the Lakeflow pipeline; sensor observes Databricks-side runs |

**Schedule:** `silver_to_gold_daily` — daily 06:00 UTC, materializes the chain in topological order.

**Sensors (auto-generated):**
- `snowflake_workspace`'s polling sensor (60s interval) emits AssetMaterializations whenever Snowflake's Dynamic Table refreshes outside of Dagster.
- `DatabricksWorkspaceComponent`'s state polling picks up Job runs that fire outside of Dagster.

**Freshness policy:** `snowflake_silver/CUSTOMER_METRICS` set to `maximum_lag_minutes: 90` (Snowflake's `TARGET_LAG=1h` + 30min headroom). Customer adds their own SLA on the Lakeflow asset based on the downstream consumer's expectations.

## About metadata refresh

If the Lakeflow pipeline reads from a UC external Iceberg table created via `CREATE TABLE ... USING ICEBERG LOCATION 's3://...'`, UC caches the metadata pointer — new Snowflake snapshots aren't auto-discovered. Fix this at the **Lakeflow SQL** level (where it costs nothing extra), not by adding a separate Dagster asset:

```sql
REFRESH FOREIGN TABLE main.silver.customer_metrics;    -- or REFRESH TABLE
CREATE OR REFRESH STREAMING TABLE customer_metrics_enriched AS
SELECT ...
FROM main.silver.customer_metrics m
LEFT JOIN main.dim.customers d USING (customer_id);
```

Alternative: connect UC to a REST catalog (Snowflake Open Catalog, Polaris, or UC-as-REST). The catalog tracks the current metadata version, and reads always see the latest snapshot without any refresh.

## Customizing for your data

The scaffold uses generic `customer_metrics` names to keep it readable. Adapting to your own workload — say, a chain-restaurant POS pipeline — requires changes in **four** places: three in your Snowflake / Databricks accounts, one in the Dagster YAML.

### 1. The Snowflake Dynamic Iceberg Table (Snowflake-side SQL)

This is where the real transformation lives. Replace the toy `customer_metrics` with the actual aggregation:

```sql
USE DATABASE RETAIL_ANALYTICS;
USE SCHEMA SILVER;

CREATE OR REPLACE DYNAMIC ICEBERG TABLE STORE_DAILY_SALES
  EXTERNAL_VOLUME = 'retail_s3_iceberg'
  CATALOG = 'SNOWFLAKE'
  BASE_LOCATION = 'retail/silver/store_daily_sales/'
  TARGET_LAG = '1 hour'
  WAREHOUSE = COMPUTE_WH
AS
  SELECT
    store_id,
    DATE_TRUNC('day', transaction_ts) AS sales_date,
    COUNT(*)                          AS transactions,
    SUM(total_amount)                 AS gross_sales,
    AVG(total_amount)                 AS avg_ticket,
    SUM(CASE WHEN m.category = 'signature_item' THEN l.line_total ELSE 0 END) AS signature_item_sales,
    COUNT(DISTINCT loyalty_id) FILTER (WHERE loyalty_id IS NOT NULL) AS loyalty_customers
  FROM RAW.POS.TRANSACTIONS t
  JOIN RAW.POS.TRANSACTION_LINES l USING (transaction_id)
  JOIN RAW.MENU                   m USING (item_id)
  WHERE transaction_ts >= DATEADD('day', -3, CURRENT_DATE)
  GROUP BY store_id, DATE_TRUNC('day', transaction_ts);
```

### 2. The Databricks Lakeflow pipeline SQL (Databricks-side)

The pipeline that wraps Snowflake's output and joins it with Databricks-side dimensions:

```sql
-- Lakeflow pipeline: store_sales_enrichment
REFRESH FOREIGN TABLE main.silver.store_daily_sales;   -- if path-based UC external table

CREATE OR REFRESH STREAMING TABLE store_sales_enriched
AS SELECT
  s.store_id,
  s.sales_date,
  s.transactions,
  s.gross_sales,
  s.avg_ticket,
  s.signature_item_sales,
  s.loyalty_customers,
  d.store_name,
  d.region,
  d.country,
  d.opening_date,
  CASE WHEN s.gross_sales > d.daily_target THEN 'above_target' ELSE 'below_target' END AS performance
FROM main.silver.store_daily_sales s                  -- ← Iceberg from Snowflake
LEFT JOIN main.dim.stores      d USING (store_id)     -- ← Databricks-resident dim
LEFT JOIN main.dim.regions     r USING (region);
```

### 3. The Databricks Job (in the Workflows UI)

Create a Job that wraps this Lakeflow pipeline (task type: **Pipeline**), name it something like `store_sales_enrichment`. Note the Job ID.

### 4. The Dagster defs (this scaffolded project)

Two YAML edits — change names + the Job ID env var:

```yaml
# defs/snowflake_silver/defs.yaml
attributes:
  database: RETAIL_ANALYTICS
  schema: SILVER
  filter_by_name_pattern: "^STORE_DAILY_SALES$"
  description: SILVER-layer retail POS — Dynamic Iceberg Table on TARGET_LAG=1h

# defs/databricks_lakeflow/defs.yaml
attributes:
  assets_by_task_key:
    store_sales_enrichment:    # ← match the Lakeflow pipeline name
      - key: databricks/lakeflow/store_sales_enriched
        deps:
          - snowflake_silver/STORE_DAILY_SALES
```

And the env vars:

```bash
export DATABRICKS_LAKEFLOW_JOB_ID=87654   # the new Job ID
```

That's the whole edit surface. The Dagster wiring shape doesn't change — only the names, the SQL, and the Job ID. The pattern (Snowflake transforms → Iceberg → Databricks Lakeflow Job, orchestrated by Dagster) is identical regardless of what's inside the SQL.

---

## What Dagster solves vs. what's outside its scope

### Dagster handles these:

- **Refresh timing.** A path-based UC external Iceberg table (created via `CREATE TABLE ... USING ICEBERG LOCATION ...`) caches its metadata pointer — Databricks reads stale data until you call `REFRESH FOREIGN TABLE` (or `REFRESH TABLE`). Put that statement at the top of the Lakeflow pipeline SQL **once**; Dagster's `deps:` + schedule guarantee the Snowflake refresh is complete before the Lakeflow Job runs. UC connected through a REST catalog (Snowflake Open Catalog / Polaris / UC-as-REST) skips the REFRESH entirely.
- **Unified freshness SLA.** Snowflake's `TARGET_LAG = '1 hour'` is one SLA; the Lakeflow pipeline has its own. Set `freshness_policy:` on both Dagster assets and you have a single place to see SLA breaches — the catalog turns the asset red, sensors / alerts pick it up.
- **Schema drift detection.** Both `snowflake_workspace` and the official `DatabricksWorkspaceComponent` emit `dagster/column_schema` metadata on each materialization. Add `build_column_schema_change_checks` to the project and Dagster auto-emits an asset check when a column appears, disappears, or changes type between runs. Catches the case where Snowflake evolves the Iceberg schema before Databricks consumers are ready.

### Outside Dagster's scope (be aware):

- **Storage retention.** Iceberg tables on S3 hold all snapshots until you `VACUUM` / expire them. Configure retention on the Snowflake side via the Dynamic Table's storage parameters; Iceberg's snapshot expiration runs from whichever engine owns the catalog.
- **Egress.** If the Iceberg storage is in one cloud and Databricks is in another, expect inter-cloud egress charges. Same-region, same-cloud is the cheap path.

## Catalog-coordinated alternative (optional — stronger consistency)

If you need stronger guarantees (concurrent writes from both engines, transactional ACID across writers, central governance), introduce a shared Iceberg REST catalog:

| Catalog | When to pick |
|---|---|
| **Snowflake Open Catalog** (managed Polaris) | You're Snowflake-first and want a managed catalog. Snowflake hosts it; Databricks federates via `CREATE CATALOG ... USING CONNECTION`. |
| **AWS Glue** | AWS-native, already have Glue tables. Snowflake writes via `CATALOG = 'glue_catalog'`; Databricks federates Glue cleanly. |
| **Databricks Unity Catalog as Iceberg REST** | Newer pattern (2024–2025). UC exposes a REST endpoint; Snowflake writes to it. Databricks reads natively. |
| **Self-hosted Apache Polaris** | Open-source, you control deployment. More ops burden. |

In the catalog-coordinated shape, you replace the storage-path `CREATE TABLE ... USING ICEBERG LOCATION ...` on the Databricks side with a federated catalog reference (e.g., `iceberg_silver.silver.customer_metrics` from a federated catalog). The Dagster wiring is identical — only the SQL on both sides changes.

## Teardown

```bash
rm -rf snowflake-iceberg-databricks-demo
# Snowflake / Databricks resources live in your accounts — clean up there manually.
```

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_snowflake_iceberg_databricks_demo.sh \
  -o setup_snowflake_iceberg_databricks_demo.sh
bash setup_snowflake_iceberg_databricks_demo.sh
```

## See also

Browse the [walkthrough index](README.md) for related demos across every component family.
