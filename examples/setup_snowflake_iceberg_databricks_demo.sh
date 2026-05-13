#!/usr/bin/env bash
# Snowflake → Iceberg → Databricks Lakeflow demo — blueprint.
#
# A common multi-vendor pipeline pattern:
#
#   Snowflake transforms                Iceberg landing                  Databricks Lakeflow
#   ─────────────────────              ──────────────────              ──────────────────────
#   Dynamic Iceberg Table              Object storage                  Lakeflow Declarative
#   writes Iceberg files               (S3/ADLS/GCS) holds              Pipeline reads via UC
#   directly to S3 via                 standard Iceberg                 external Iceberg table
#   external volume                    metadata + data files            pointed at the same
#                                                                       storage path
#
# Iceberg is an open table format — the metadata + data files in object
# storage are self-describing. Databricks UC registers the table by storage
# location; no intermediate REST-catalog server required. A catalog-
# coordinated alternative is documented in the walkthrough.
#
# Dagster wires both sides via the existing community components:
#   - snowflake_workspace          → imports Snowflake dynamic tables as assets
#   - external_snowflake_table     → declares the Iceberg landing for explicit lineage
#   - databricks_workspace         → imports the Lakeflow (DLT) pipeline + its outputs as assets
#
# NOTE: This setup script builds the Dagster project + defs.yaml. It does NOT
# stand up Snowflake / Databricks — that requires real accounts. Pre-reqs
# (external volume, UC external Iceberg table SQL, Lakeflow pipeline) are in
# snowflake_iceberg_databricks.md.
#
# Validation status:
#   - dg check: passes against the YAML
#   - End-to-end materialization: requires customer Snowflake + Databricks
#     accounts with Iceberg + Unity Catalog federation set up. NOT validated
#     in this repo. Common, production-shape pattern — bring your own creds.

set -euo pipefail

PROJECT_DIR="${1:-snowflake-iceberg-databricks-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q snowflake-connector-python databricks-sdk
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 3 community components"
$CLI add snowflake_workspace      --auto-install
$CLI add external_snowflake_table --auto-install
$CLI add databricks_workspace     --auto-install

# Suppress the auto-installed example defs that would conflict
rm -rf "src/$PKG/defs/snowflake_workspace" \
       "src/$PKG/defs/external_snowflake_table" \
       "src/$PKG/defs/databricks_workspace"

mkdir -p "src/$PKG/defs/snowflake_silver" \
         "src/$PKG/defs/customer_metrics_iceberg" \
         "src/$PKG/defs/databricks_lakeflow"

echo ">>> Writing defs.yaml"

# --- 1. Snowflake side: import Dynamic Tables + Tasks from the SILVER schema.
# In Snowflake, the Dynamic Iceberg Table `CUSTOMER_METRICS` is defined as:
#
#   CREATE OR REPLACE DYNAMIC ICEBERG TABLE CUSTOMER_METRICS
#     EXTERNAL_VOLUME = 'my_s3_iceberg_volume'
#     CATALOG = 'SNOWFLAKE'                       -- Snowflake-managed
#     BASE_LOCATION = 'analytics/customer_metrics/'
#     TARGET_LAG = '1 hour'
#     WAREHOUSE = COMPUTE_WH
#   AS
#     SELECT customer_id,
#            COUNT(DISTINCT order_id) AS lifetime_orders,
#            SUM(total)               AS lifetime_revenue,
#            MAX(order_date)          AS last_order_date
#     FROM RAW.PUBLIC.ORDERS
#     GROUP BY customer_id;
#
# The Iceberg files land at s3://my-iceberg-bucket/analytics/customer_metrics/
# (metadata.json + Parquet data files + manifest lists — standard Iceberg).
# Databricks UC reads them directly via storage location.
#
# snowflake_workspace imports this as an observable asset that materializes
# whenever the Dynamic Table refreshes.
cat > "src/$PKG/defs/snowflake_silver/defs.yaml" <<EOF
type: $PKG.components.snowflake_workspace.component.SnowflakeWorkspaceComponent
attributes:
  account: "{{ env('SNOWFLAKE_ACCOUNT') }}"
  user: "{{ env('SNOWFLAKE_USER') }}"
  password: "{{ env('SNOWFLAKE_PASSWORD') }}"
  warehouse: COMPUTE_WH
  database: ANALYTICS
  schema: SILVER

  # The Dynamic Table that writes the Iceberg landing table is what we want
  # to materialize on a schedule. Tasks are imported too in case the customer
  # has additional orchestration via Snowflake Tasks.
  import_dynamic_tables: true
  import_tasks: true
  import_streams: false
  import_stored_procedures: false
  import_materialized_views: false
  import_snowpipes: false
  import_stages: false
  import_external_tables: false
  import_alerts: false
  import_openflow_flows: false

  # Filter to just the SILVER-layer entities (customer's naming convention).
  filter_by_name_pattern: "^(CUSTOMER_METRICS|REFRESH_CUSTOMER_METRICS)\$"

  # Auto-generate a sensor that polls Snowflake for Dynamic Table refreshes
  # and emits AssetMaterializations into Dagster's catalog.
  generate_sensor: true
  poll_interval_seconds: 60

  group_name: snowflake_silver
  description: SILVER-layer Snowflake — Dynamic Tables writing Iceberg
EOF

# --- 2. Iceberg landing table: declare as an explicit external asset so
# lineage is visible in the catalog. Snowflake writes the Iceberg files
# under the namespace ANALYTICS.SILVER directly to S3. Databricks UC
# reads the same S3 path as an external Iceberg table — no REST-catalog
# server required.
cat > "src/$PKG/defs/customer_metrics_iceberg/defs.yaml" <<EOF
type: $PKG.components.external_snowflake_table.component.ExternalSnowflakeTableAsset
attributes:
  asset_key: silver/customer_metrics_iceberg
  account: "{{ env('SNOWFLAKE_ACCOUNT') }}"
  database: ANALYTICS
  schema_name: SILVER
  table_name: CUSTOMER_METRICS
  group_name: iceberg_landing
  description: |
    Snowflake-managed Iceberg table. Registered in Snowflake Open Catalog
    under ANALYTICS.SILVER. Backed by external volume on S3.

    Lineage: snowflake/ANALYTICS/SILVER/CUSTOMER_METRICS (Snowflake Dynamic
    Table) → this asset → databricks/lakeflow/customer_metrics_enriched
    (Lakeflow pipeline reads it via UC federation).
EOF

# --- 3. Databricks side: import Lakeflow Declarative Pipeline as an asset.
# Pre-req: register the Iceberg table in Unity Catalog by storage location:
#
#   CREATE TABLE main.silver.customer_metrics
#     USING ICEBERG
#     LOCATION 's3://my-iceberg-bucket/analytics/customer_metrics/';
#
# Then a Lakeflow Declarative Pipeline named "customer_metrics_enrichment"
# reads it (illustrative SQL):
#
#   CREATE OR REFRESH STREAMING TABLE customer_metrics_enriched
#   AS SELECT
#     m.customer_id, m.lifetime_orders, m.lifetime_revenue,
#     CASE WHEN m.lifetime_revenue > 1000 THEN 'high' ELSE 'low' END AS tier,
#     d.region, d.signup_date
#   FROM main.silver.customer_metrics m
#   LEFT JOIN main.dim.customers d USING (customer_id);
#
# databricks_workspace imports this pipeline as a materializable asset.
cat > "src/$PKG/defs/databricks_lakeflow/defs.yaml" <<EOF
type: $PKG.components.databricks_workspace.component.DatabricksWorkspaceComponent
attributes:
  workspace_url: "{{ env('DATABRICKS_HOST') }}"
  access_token: "{{ env('DATABRICKS_TOKEN') }}"

  # Only import Lakeflow pipelines (formerly DLT). The component's flag is
  # `import_dlt_pipelines` — DLT = Lakeflow Declarative Pipelines as of 2025.
  import_dlt_pipelines: true
  import_jobs: false
  import_notebooks: false
  import_model_endpoints: false

  # Filter to the specific Lakeflow pipeline we care about
  filter_by_name_pattern: "^customer_metrics_enrichment\$"

  # Auto-generate a sensor so Dagster observes Lakeflow runs that fire
  # outside of Dagster (e.g., from Databricks' own schedule)
  generate_sensor: true
  poll_interval_seconds: 60

  group_name: databricks_gold
  description: Databricks Lakeflow pipelines that consume the Iceberg landing

  # Override the pipeline's asset key + add explicit dependency on the
  # Iceberg landing for visible lineage in the Dagster catalog
  assets_by_pipeline_name:
    customer_metrics_enrichment:
      key: databricks/lakeflow/customer_metrics_enriched
      group_name: databricks_gold
      description: |
        Enriches the Iceberg landing with dim_customers, adds tier classification.
      deps:
        - silver/customer_metrics_iceberg
      metadata:
        catalog: snowflake_open_catalog
        federated_table: ANALYTICS.SILVER.CUSTOMER_METRICS
EOF

cat <<MSG

>>> Setup complete.

This is a BLUEPRINT — it scaffolds the Dagster project + defs.yaml. It does
NOT validate end-to-end (that requires a real Snowflake account with Iceberg
configured, a real Databricks workspace with Unity Catalog Iceberg federation,
and a shared Iceberg catalog).

To wire up your environment:

  1. Read examples/snowflake_iceberg_databricks.md for the Snowflake +
     Databricks prerequisites (external volume, catalog integration,
     UC federation setup, sample Lakeflow SQL).

  2. Set env vars before running dg check / dg dev:

       export SNOWFLAKE_ACCOUNT=myorg-us-east-1
       export SNOWFLAKE_USER=dagster_user
       export SNOWFLAKE_PASSWORD=...
       export DATABRICKS_HOST=https://dbc-xxx.cloud.databricks.com
       export DATABRICKS_TOKEN=...

  3. Validate the YAML loads:
       cd $PROJECT_DIR && uv run dg check defs

  4. Launch the UI:
       cd $PROJECT_DIR && uv run dg dev

  5. In the Dagster catalog you'll see:
       - snowflake_silver/CUSTOMER_METRICS         (Dynamic Table)
       - silver/customer_metrics_iceberg            (Iceberg landing)
       - databricks/lakeflow/customer_metrics_enriched (Lakeflow)
     with lineage flowing left to right.
MSG
