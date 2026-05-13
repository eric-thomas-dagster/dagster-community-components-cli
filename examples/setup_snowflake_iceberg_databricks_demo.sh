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
# Official Dagster integrations — these provide DatabricksWorkspaceComponent
# and SnowflakeResource. The community snowflake_workspace below sits on top
# of dagster-snowflake.
uv add -q dagster-databricks dagster-snowflake snowflake-connector-python databricks-sdk
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 2 community components"
echo "    (Snowflake side: no official DynamicTable/Task importer;"
echo "     Databricks side: OFFICIAL component from dagster-databricks)"
$CLI add snowflake_workspace --auto-install
$CLI add cron_schedule       --auto-install

# Suppress the auto-installed example defs that would conflict
rm -rf "src/$PKG/defs/snowflake_workspace" \
       "src/$PKG/defs/cron_schedule"

mkdir -p "src/$PKG/defs/snowflake_silver" \
         "src/$PKG/defs/databricks_lakeflow" \
         "src/$PKG/defs/refresh_schedule"

echo ">>> Writing defs.yaml"

# --- 1. Snowflake side: import Dynamic Tables + Tasks from the SILVER schema.
# Uses the COMMUNITY snowflake_workspace component — there is no official
# dagster-snowflake component for Dynamic Tables / Tasks / Streams. The
# community component sits on top of dagster-snowflake's SnowflakeResource.
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

  # The Dynamic Iceberg Table — what we want to materialize as our SILVER
  # output. Tasks imported too so any Snowflake-side orchestration shows up.
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

  # Freshness policy — tells Dagster (+ alerts) when this is stale.
  # Snowflake's TARGET_LAG='1 hour' is the SLA; give Dagster 30min headroom.
  freshness_max_lag_minutes: 90

  group_name: snowflake_silver
  description: SILVER-layer Snowflake — Dynamic Iceberg Table refreshed on TARGET_LAG
EOF

# --- 2. Databricks side: OFFICIAL DatabricksWorkspaceComponent from
# dagster-databricks. Imports Databricks Jobs (with task-level assets).
# Lakeflow pipelines are typically wrapped in a Job (task type =
# pipeline_task) so the official component picks them up via the Job.
#
# Pre-req in Databricks:
#   1. Register the Iceberg table in UC by storage location:
#      CREATE TABLE main.silver.customer_metrics
#        USING ICEBERG
#        LOCATION 's3://my-iceberg-bucket/analytics/customer_metrics/';
#   2. Create a Lakeflow Declarative Pipeline that reads it (SQL like):
#      CREATE OR REFRESH STREAMING TABLE customer_metrics_enriched AS
#      SELECT m.*, d.region FROM main.silver.customer_metrics m
#      LEFT JOIN main.dim.customers d USING (customer_id);
#   3. Wrap the pipeline in a Job (Databricks UI: New Job → Task type
#      "Pipeline" → pick customer_metrics_enrichment). Note the job_id.
cat > "src/$PKG/defs/databricks_lakeflow/defs.yaml" <<EOF
type: dagster_databricks.DatabricksWorkspaceComponent
attributes:
  workspace:
    host: "{{ env('DATABRICKS_HOST') }}"
    token: "{{ env('DATABRICKS_TOKEN') }}"

  # Replace with the Databricks Job ID that wraps the Lakeflow pipeline.
  # Find it in the Databricks UI → Workflows → your job → Job details.
  databricks_filter:
    include_jobs:
      job_ids:
        - {{ env('DATABRICKS_LAKEFLOW_JOB_ID') }}

  # Map the Lakeflow Job's task → a Dagster asset key, with an explicit
  # dependency on the Snowflake Dynamic Iceberg Table for visible lineage.
  # Sequencing: Snowflake materializes first, then this asset triggers the
  # Databricks Job (which runs the Lakeflow pipeline).
  #
  # If your Lakeflow pipeline reads from a UC external Iceberg table created
  # via `CREATE TABLE ... USING ICEBERG LOCATION '...'`, add a `REFRESH
  # FOREIGN TABLE` (or `REFRESH TABLE`) statement at the top of the
  # pipeline SQL so it sees Snowflake's latest snapshot. UC connected
  # through a REST catalog (Snowflake Open Catalog / Polaris / UC-as-REST)
  # auto-refreshes — no extra step needed.
  assets_by_task_key:
    customer_metrics_enrichment:
      - key: databricks/lakeflow/customer_metrics_enriched
        group_name: databricks_gold
        description: |
          Lakeflow Declarative Pipeline output. Enriches the Iceberg
          landing with dim_customers + tier classification.
        deps:
          - snowflake_silver/CUSTOMER_METRICS
        metadata:
          databricks_job_id: "{{ env('DATABRICKS_LAKEFLOW_JOB_ID') }}"
EOF

# --- 3. Schedule — daily at 06:00 UTC. Materializes the chain end-to-end:
# Snowflake Dynamic Table refresh → Databricks Job (Lakeflow pipeline).
cat > "src/$PKG/defs/refresh_schedule/defs.yaml" <<EOF
type: $PKG.components.cron_schedule.component.CronScheduleComponent
attributes:
  schedule_name: silver_to_gold_daily
  cron_expression: "0 6 * * *"
  execution_timezone: UTC
  asset_keys:
    - snowflake_silver/CUSTOMER_METRICS
    - databricks/lakeflow/customer_metrics_enriched
  default_status: STOPPED
  tags:
    pipeline: snowflake_to_databricks_lakeflow
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
       export DATABRICKS_LAKEFLOW_JOB_ID=12345    # the Job that wraps the Lakeflow pipeline

  3. Validate the YAML loads:
       cd $PROJECT_DIR && uv run dg check defs

  4. Launch the UI:
       cd $PROJECT_DIR && uv run dg dev

  5. In the Dagster catalog you'll see two assets with lineage:
       - snowflake_silver/CUSTOMER_METRICS              (Snowflake Dynamic Iceberg Table)
       - databricks/lakeflow/customer_metrics_enriched   (Databricks Job wrapping Lakeflow)
         deps: snowflake_silver/CUSTOMER_METRICS

     Plus a schedule (silver_to_gold_daily, 06:00 UTC) that materializes
     the chain in topological order. Sensors auto-generated by each
     component observe out-of-band runs (Snowflake's TARGET_LAG-driven
     refreshes, Databricks-side reruns).

     If your Lakeflow pipeline reads from a UC external Iceberg table
     created via 'CREATE TABLE ... USING ICEBERG LOCATION ...', add
     'REFRESH FOREIGN TABLE main.silver.customer_metrics' at the top of
     the Lakeflow SQL — UC caches the metadata pointer for path-based
     external tables. UC connected through a REST catalog (Snowflake Open
     Catalog / Polaris) auto-refreshes; no extra step needed.
MSG
