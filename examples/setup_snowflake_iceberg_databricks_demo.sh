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
  # CUSTOMIZE: credentials via env vars — set before running dg check / dg dev
  account: "{{ env('SNOWFLAKE_ACCOUNT') }}"
  user: "{{ env('SNOWFLAKE_USER') }}"
  password: "{{ env('SNOWFLAKE_PASSWORD') }}"

  # CUSTOMIZE: replace with your Snowflake warehouse / database / schema
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

  # CUSTOMIZE: regex that matches the Dynamic Iceberg Table name(s) you want
  # to expose as Dagster assets. Default below matches the toy CUSTOMER_METRICS
  # example — change to ^(YOUR_TABLE_1|YOUR_TABLE_2)\$ for your real tables.
  filter_by_name_pattern: "^(CUSTOMER_METRICS|REFRESH_CUSTOMER_METRICS)\$"

  # Auto-generate a sensor that polls Snowflake for Dynamic Table refreshes
  # and emits AssetMaterializations into Dagster's catalog.
  generate_sensor: true
  poll_interval_seconds: 60

  # CUSTOMIZE: freshness SLA. Snowflake's TARGET_LAG='1 hour' is the SLA;
  # this gives Dagster 30min headroom. Adjust based on your TARGET_LAG.
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
  # CUSTOMIZE: credentials via env vars
  workspace:
    host: "{{ env('DATABRICKS_HOST') }}"
    token: "{{ env('DATABRICKS_TOKEN') }}"

  # CUSTOMIZE: the Databricks Job ID that wraps the Lakeflow pipeline.
  # Find it in the Databricks UI → Workflows → your Job → Job details.
  # Add multiple job_ids if you want to import several Lakeflow Jobs at once.
  databricks_filter:
    include_jobs:
      job_ids:
        - {{ env('DATABRICKS_LAKEFLOW_JOB_ID') }}

  # CUSTOMIZE: this maps a Databricks Job's task_key → Dagster asset key.
  # The task_key on the left ("customer_metrics_enrichment") must match the
  # actual task_key inside your Job in Databricks. The deps line wires
  # cross-engine lineage from the Snowflake Dynamic Iceberg Table.
  #
  # If your Lakeflow pipeline reads from a UC external Iceberg table created
  # via 'CREATE TABLE ... USING ICEBERG LOCATION ...', add a
  # 'REFRESH FOREIGN TABLE' (or 'REFRESH TABLE') statement at the top of
  # the pipeline SQL so it sees Snowflake's latest snapshot. UC connected
  # through a REST catalog (Snowflake Open Catalog / Polaris / UC-as-REST)
  # auto-refreshes — no extra step needed.
  assets_by_task_key:
    customer_metrics_enrichment:    # ← CUSTOMIZE: match your Job's actual task_key
      - key: databricks/lakeflow/customer_metrics_enriched   # ← CUSTOMIZE: pick the asset key
        group_name: databricks_gold
        description: |
          Lakeflow Declarative Pipeline output. CUSTOMIZE this description
          for your actual pipeline.
        deps:
          - snowflake_silver/CUSTOMER_METRICS                # ← CUSTOMIZE: match Snowflake table
        metadata:
          databricks_job_id: "{{ env('DATABRICKS_LAKEFLOW_JOB_ID') }}"
EOF

# --- 3. Schedule — daily at 06:00 UTC. Materializes the chain end-to-end:
# Snowflake Dynamic Table refresh → Databricks Job (Lakeflow pipeline).
cat > "src/$PKG/defs/refresh_schedule/defs.yaml" <<EOF
type: $PKG.components.cron_schedule.component.CronScheduleComponent
attributes:
  schedule_name: silver_to_gold_daily
  # CUSTOMIZE: cron expression + timezone for your refresh cadence
  cron_expression: "0 6 * * *"
  execution_timezone: UTC
  # CUSTOMIZE: these asset keys MUST match what you set above. If you
  # changed the Snowflake table name or the Databricks asset key, mirror
  # the change here.
  asset_keys:
    - snowflake_silver/CUSTOMER_METRICS
    - databricks/lakeflow/customer_metrics_enriched
  default_status: STOPPED
  tags:
    pipeline: snowflake_to_databricks_lakeflow
EOF

# --- Write a CUSTOMIZE.md punch list inside the project root.
cat > CUSTOMIZE.md <<'CUSTOMIZE_EOF'
# Customize before running

This project was scaffolded by `setup_snowflake_iceberg_databricks_demo.sh`
as a **blueprint**. The YAML uses placeholder table / pipeline / asset names
from a toy `customer_metrics` example. You MUST replace them with your own
identifiers before `dg dev` will produce a working pipeline.

## Step 1 — Set credentials (env vars)

```bash
export SNOWFLAKE_ACCOUNT=myorg-us-east-1
export SNOWFLAKE_USER=dagster_user
export SNOWFLAKE_PASSWORD=...
export DATABRICKS_HOST=https://dbc-xxx.cloud.databricks.com
export DATABRICKS_TOKEN=dapi...
export DATABRICKS_LAKEFLOW_JOB_ID=12345     # the Job ID that wraps your Lakeflow pipeline
```

## Step 2 — Edit the YAML (3 files)

Every line that must change is marked with `# CUSTOMIZE:` in the YAML.

### `src/<pkg>/defs/snowflake_silver/defs.yaml`

- `warehouse: COMPUTE_WH` — your Snowflake warehouse
- `database: ANALYTICS` — your Snowflake database
- `schema: SILVER` — your Snowflake schema
- `filter_by_name_pattern: "^(CUSTOMER_METRICS|REFRESH_CUSTOMER_METRICS)$"` — regex matching the Dynamic Iceberg Table name(s) you want to expose as assets. **The default will only match if you literally have a table called CUSTOMER_METRICS.**
- `freshness_max_lag_minutes: 90` — match your Snowflake TARGET_LAG + headroom

### `src/<pkg>/defs/databricks_lakeflow/defs.yaml`

- `assets_by_task_key.customer_metrics_enrichment` — the task_key on the left **must match the actual task_key inside your Databricks Job** (visible in the Workflows UI → Job details → Tasks tab). If your Job has a task named "etl_main", change this line to `etl_main:`.
- `key: databricks/lakeflow/customer_metrics_enriched` — the Dagster asset key you want for the Lakeflow output. Pick anything.
- `deps: [snowflake_silver/CUSTOMER_METRICS]` — must match the asset key the Snowflake side exposes (which derives from your Dynamic Table name).

### `src/<pkg>/defs/refresh_schedule/defs.yaml`

- `cron_expression`, `execution_timezone` — your refresh cadence
- `asset_keys:` — must mirror whatever you set in the two files above. If you renamed the assets, rename them here too.

## Step 3 — Snowflake / Databricks setup

See `examples/snowflake_iceberg_databricks.md` for:
- The `CREATE OR REPLACE DYNAMIC ICEBERG TABLE` SQL on the Snowflake side
- The `CREATE TABLE ... USING ICEBERG LOCATION` on the Databricks UC side
- The Lakeflow Declarative Pipeline SQL
- Wrapping the Lakeflow pipeline in a Job (so the official `DatabricksWorkspaceComponent` picks it up)
- Auth and catalog choices (Snowflake-managed vs Snowflake Open Catalog vs Glue vs Polaris)

## Step 4 — Validate + run

```bash
uv run dg check defs    # validates YAML + components load
uv run dg dev           # → http://localhost:3000
```

If `dg check` fails: most likely an asset key mismatch between the deps in
databricks_lakeflow/defs.yaml and the snowflake_silver Dynamic Table name.

If `dg dev` loads but the Snowflake asset doesn't appear: your
`filter_by_name_pattern` regex didn't match any table.

If the asset appears but materialization fails: most likely a credential
or network issue — check env vars and Snowflake/Databricks network policies.
CUSTOMIZE_EOF

cat <<MSG

============================================================================
>>> Setup complete — BUT THIS WILL NOT RUN OUT OF THE BOX.
============================================================================

This is a BLUEPRINT, not a runnable demo. It scaffolds the Dagster project
+ defs.yaml with PLACEHOLDER names from a toy customer_metrics example.

You MUST customize before 'dg dev' produces a working pipeline. The script
just wrote $PROJECT_DIR/CUSTOMIZE.md — read it. It's a punch list of:

  - the env vars to set (credentials + Job ID)
  - the YAML lines to edit (marked '# CUSTOMIZE:' in each defs.yaml)
  - the Snowflake / Databricks SQL pre-reqs (in the walkthrough)

Architectural overview, gotchas, and customer-customization walkthrough:
  examples/snowflake_iceberg_databricks.md

What this script DID do:
  - Scaffolded a Dagster project
  - Added dagster-databricks, dagster-snowflake, snowflake-connector-python, databricks-sdk
  - Installed snowflake_workspace + cron_schedule community components
  - Wrote three defs.yaml files, each annotated with '# CUSTOMIZE:' markers
  - Wrote $PROJECT_DIR/CUSTOMIZE.md (the punch list)

What this script DID NOT do:
  - Prompt for or store any credentials
  - Run dg check or dg dev
  - Stand up Snowflake / Databricks resources (you do that)
  - Match your real table/pipeline/Job names (you edit that)

Next steps:
  cd $PROJECT_DIR
  cat CUSTOMIZE.md          # the punch list
  \$EDITOR src/$PKG/defs/snowflake_silver/defs.yaml
  \$EDITOR src/$PKG/defs/databricks_lakeflow/defs.yaml
  \$EDITOR src/$PKG/defs/refresh_schedule/defs.yaml
  # set env vars
  uv run dg check defs
  uv run dg dev
MSG
