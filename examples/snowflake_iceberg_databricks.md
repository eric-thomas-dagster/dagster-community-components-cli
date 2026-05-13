# Snowflake → Iceberg → Databricks Lakeflow — blueprint

A common production pattern: **Snowflake** does the heavy SQL transformations on raw data, lands the result as an **Apache Iceberg** table in a shared catalog, and **Databricks Lakeflow Declarative Pipelines** (formerly DLT) pick that table up via Unity Catalog federation to feed downstream models / dashboards / lakehouse silver-gold layers.

Dagster orchestrates both sides and surfaces the cross-engine lineage in one catalog.

> **Validation status:** the Dagster wiring is buildable and passes `dg check` once env vars are set. The end-to-end pipeline has **not** been validated in this repo — it requires real Snowflake + Databricks accounts and a configured shared Iceberg catalog. This is a common, production-shape pattern, presented as a blueprint to copy into your environment.

## Architecture

```
                    ┌────────────────────────────────────────────────┐
                    │  SNOWFLAKE (transformations + Iceberg writes)  │
                    │                                                │
   RAW.ORDERS  ─┐  ┌─►  Dynamic Iceberg Table                       │
   RAW.CUSTOMERS├──┤    SILVER.CUSTOMER_METRICS                     │
                 │  │    (writes to S3 via external volume +         │
                 │  │     registers in Snowflake Open Catalog)       │
                 │  └─────────────────────┬──────────────────────────┘
                 │                        │
                 │            ┌───────────▼────────────┐
                 │            │  SHARED ICEBERG CATALOG │
                 │            │  Snowflake Open Catalog │ ◄── Apache Polaris under the hood
                 │            │  ANALYTICS.SILVER.*     │
                 │            └───────────┬────────────┘
                 │                        │
                 │  ┌─────────────────────▼──────────────────────────┐
                 │  │  DATABRICKS (Unity Catalog Iceberg federation) │
                 │  │                                                │
                 │  │  External table:  uc_catalog.silver.           │
                 │  │                   customer_metrics             │
                 │  │              │                                 │
                 │  │              ▼                                 │
                 │  │  Lakeflow Declarative Pipeline                 │
                 │  │  customer_metrics_enrichment                   │
                 │  │   → joins with main.dim.customers              │
                 │  │   → writes gold.customer_metrics_enriched      │
                 │  └────────────────────────────────────────────────┘
                 │                        │
                 │  ┌─────────────────────▼──────────────────────────┐
                 │  │   DAGSTER (cross-engine orchestrator)          │
                 │  │                                                │
                 │  │   snowflake_workspace ──┐                      │
                 │  │   external_snowflake_table  ──┐                │
                 │  │   databricks_workspace ──────────►  catalog +  │
                 │  │                                    sensors +   │
                 │  │                                    lineage     │
                 │  └────────────────────────────────────────────────┘
```

## Components used

| Component | Role |
|---|---|
| `snowflake_workspace` | Imports Snowflake Dynamic Tables / Tasks / Streams / Snowpipes as Dagster assets. The Dynamic Iceberg Table becomes a materializable asset; a sensor observes refreshes. |
| `external_snowflake_table` | Declares the Iceberg landing table as an explicit external asset so the Iceberg handoff is visible in the lineage graph. |
| `databricks_workspace` | Imports Databricks Lakeflow Declarative Pipelines (the flag is still `import_dlt_pipelines:` — DLT was renamed to Lakeflow in 2025), jobs, notebooks, ML endpoints. |

All three are wrappers around the **official** `dagster-snowflake` / `dagster-databricks` packages — no reinvention.

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

3. **Catalog integration** — pick one:

   - **Snowflake Open Catalog** (recommended for Databricks federation; managed Polaris):
     ```sql
     CREATE OR REPLACE CATALOG INTEGRATION snowflake_open_catalog
       CATALOG_SOURCE = POLARIS
       TABLE_FORMAT = ICEBERG
       CATALOG_NAMESPACE = 'ANALYTICS'
       REST_CONFIG = (
         CATALOG_URI = 'https://<account>.snowflakecomputing.com/polaris/api/catalog'
         WAREHOUSE = 'my_open_catalog_warehouse'
       )
       REST_AUTHENTICATION = (...)
       ENABLED = TRUE;
     ```
   - **AWS Glue** (if your customer is AWS-centric):
     ```sql
     CREATE OR REPLACE CATALOG INTEGRATION glue_catalog
       CATALOG_SOURCE = GLUE
       TABLE_FORMAT = ICEBERG
       GLUE_AWS_ROLE_ARN = 'arn:aws:iam::...'
       GLUE_CATALOG_ID = '123456789012'
       GLUE_REGION = 'us-east-1'
       CATALOG_NAMESPACE = 'analytics'
       ENABLED = TRUE;
     ```
   - **Self-hosted Apache Polaris**: standard Iceberg REST catalog config.

4. **The actual Dynamic Iceberg Table** — the thing the Snowflake side materializes:
   ```sql
   USE DATABASE ANALYTICS;
   USE SCHEMA SILVER;

   CREATE OR REPLACE DYNAMIC ICEBERG TABLE CUSTOMER_METRICS
     EXTERNAL_VOLUME = 'my_s3_iceberg_volume'
     CATALOG = 'snowflake_open_catalog'
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

### Databricks side

1. **Unity Catalog** workspace with metastore.
2. **Iceberg federation** to the Snowflake Open Catalog (or whatever you picked for the catalog):
   ```sql
   -- In a Databricks SQL editor or notebook
   CREATE CATALOG iceberg_silver
     USING CONNECTION my_polaris_connection
     OPTIONS (catalog = 'snowflake_open_catalog');
   ```
   (See Databricks docs for the exact `CREATE CONNECTION` syntax for Polaris / Glue.)

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
   FROM iceberg_silver.silver.customer_metrics m   -- ← federated Iceberg table
   LEFT JOIN main.dim.customers d USING (customer_id);
   ```

4. **A personal access token** with permissions to read pipelines + trigger runs.

## Setup (Dagster side)

```bash
./setup_snowflake_iceberg_databricks_demo.sh
cd snowflake-iceberg-databricks-demo

# Env vars (paste your real values)
export SNOWFLAKE_ACCOUNT=myorg-us-east-1
export SNOWFLAKE_USER=dagster_user
export SNOWFLAKE_PASSWORD=...
export DATABRICKS_HOST=https://dbc-xxx.cloud.databricks.com
export DATABRICKS_TOKEN=...

# Validate the YAML loads + components resolve
uv run dg check defs

# Launch the UI
uv run dg dev
# → http://localhost:3000
```

## What you'll see in the catalog

Three assets, with lineage flowing left → right:

1. **`snowflake_silver/CUSTOMER_METRICS`** — Snowflake Dynamic Iceberg Table (observable; refreshes are detected by the sensor every 60s).
2. **`silver/customer_metrics_iceberg`** — external asset representing the Iceberg landing in the shared catalog. Lineage only — no execution.
3. **`databricks/lakeflow/customer_metrics_enriched`** — Databricks Lakeflow pipeline output. Materializing this kicks off the Lakeflow pipeline; a sensor also observes external runs.

## Trade-offs & gotchas (read before you demo)

- **Federation latency.** When Snowflake refreshes the Iceberg Dynamic Table, Databricks doesn't see new data until the Iceberg catalog is refreshed on Databricks' side (UC federation polls periodically; `REFRESH FOREIGN CATALOG` is the manual trigger). Dagster's sensor doesn't make this faster — it just observes when each side has materialized.
- **Schema evolution.** If Snowflake evolves the Iceberg schema (adds a column), Databricks' federated view needs to refresh metadata. Lakeflow pipelines that `SELECT *` will pick it up; explicit column lists may break.
- **Storage costs.** Iceberg tables on S3 hold all snapshots until you `VACUUM` / expire them. The Snowflake side has knobs for this; Databricks' federated reads don't manage retention.
- **Two SLAs.** Snowflake Dynamic Table's `TARGET_LAG = '1 hour'` is a Snowflake-side SLA; the Lakeflow pipeline has its own. Dagster gives you one place to set freshness expectations across both — use `freshness_policy:` on the assets.
- **Egress.** If the Iceberg storage is in one cloud and Databricks is in another, expect egress. Same-region same-cloud is the cheap path.

## Alternatives if Snowflake Open Catalog doesn't fit

| Catalog | When to pick |
|---|---|
| **Snowflake Open Catalog** (managed Polaris) | Default if you have Snowflake — no extra infra, native to Snowflake. |
| **AWS Glue** | Customer is AWS-native and already has Glue tables. Databricks federates Glue cleanly. |
| **Self-hosted Apache Polaris** | Open-source, you control deployment. More ops burden. |
| **Tabular** (acquired by Databricks) | Often the answer if you're going Databricks-first; Snowflake still federates. |

The Dagster wiring is the same shape regardless — only the `CATALOG` argument in the Snowflake `CREATE DYNAMIC ICEBERG TABLE` and the Databricks federation config change.

## Cleanup

```bash
rm -rf snowflake-iceberg-databricks-demo
# Snowflake / Databricks resources live in your accounts — clean up there manually.
```
