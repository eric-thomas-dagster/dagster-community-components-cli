#!/usr/bin/env bash
# Snowpark Pipeline demo — full compute pushdown to Snowflake.
#
# VALIDATE_MODE: check_defs_only
# (Snowflake creds required to actually materialize. The setup script
# scaffolds a complete demo that loads + validates with `dg check defs`;
# `dg launch` would need real Snowflake credentials in env vars to run.)
#
# WHAT THIS DEMONSTRATES
#   snowpark_pipeline: multi-step Snowpark DataFrame chain compiled into
#   ONE Snowflake SQL statement. The whole pipeline runs inside the
#   Snowflake compute warehouse — no data ever flows through Python.
#
#   Components exercised (1):
#     - snowpark_pipeline   ONE asset reading raw.orders from Snowflake,
#                           running a 5-op chain (filter → with_columns →
#                           group_by → sort → limit), writing
#                           analytics.top_5_categories_revenue back to
#                           Snowflake. All compute server-side.
#
# REQUIRES
#   - A Snowflake account (free trial works: snowflake.com/start)
#   - SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_PASSWORD env vars
#   - Source table at RAW.ORDERS in your target database (see "seeding"
#     section in the trailing MSG)
#
# COST: charged to your Snowflake compute warehouse (XS works fine for
#       this volume — a few credits at most).

set -euo pipefail
PROJECT_DIR="${1:-snowpark-pipeline-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q 'yarl<1.24'
uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q 'snowflake-snowpark-python>=1.10.0'

CLI="uvx --from dagster-community-components-cli dagster-component"
$CLI add snowpark_pipeline --auto-install
rm -rf "src/$PKG/defs/snowpark_pipeline"

write_yaml() {
  local name="$1"; shift
  local body="$1"; shift
  mkdir -p "src/$PKG/defs/$name"
  printf "%s\n" "$body" > "src/$PKG/defs/$name/defs.yaml"
}

# snowpark_pipeline — 5-op DataFrame chain compiled to ONE Snowflake SQL stmt
write_yaml "top_5_categories_by_revenue" "type: $PKG.components.snowpark_pipeline.component.SnowparkPipelineComponent
attributes:
  asset_name: top_5_categories_by_revenue
  connection:
    account_env_var:  SNOWFLAKE_ACCOUNT
    user_env_var:     SNOWFLAKE_USER
    password_env_var: SNOWFLAKE_PASSWORD
    role:      TRANSFORMER
    warehouse: COMPUTE_WH
    database:  ANALYTICS
    schema:    STAGING
  source:
    kind: table
    table: RAW.ORDERS
  operations:
    - op: filter
      predicate: \"STATUS = 'paid' AND AMOUNT > 100\"
    - op: with_columns
      expressions:
        is_high_value: \"CASE WHEN AMOUNT > 1000 THEN TRUE ELSE FALSE END\"
    - op: group_by
      group_by: [REGION, CATEGORY]
      aggregations:
        revenue:     {col: AMOUNT,   agg: sum}
        order_count: {col: ORDER_ID, agg: count}
    - op: sort
      by: [REGION, revenue]
      descending: [false, true]
    - op: limit
      n: 5
  sink:
    kind: table
    table: ANALYTICS.TOP_5_CATEGORIES_BY_REVENUE
    mode: overwrite
  group_name: snowpark_pipeline_demo"

cat <<MSG

>>> Setup complete (check_defs_only — Snowflake creds needed for full run).

Validate the project loads cleanly (no creds needed for this):
    cd $PROJECT_DIR
    uv run dg check defs

To actually materialize:
  1. Export Snowflake credentials:
       export SNOWFLAKE_ACCOUNT='myorg-myaccount'           # or full URL
       export SNOWFLAKE_USER='ETL_USER'
       export SNOWFLAKE_PASSWORD='...'
  2. Make sure RAW.ORDERS exists in your ANALYTICS database. Quick seed:
       USE DATABASE ANALYTICS;
       USE SCHEMA STAGING;
       CREATE OR REPLACE TABLE RAW.ORDERS AS
       SELECT * FROM (
         VALUES
           (1, 'paid',    'us-east', 'electronics', 1500.00, 100),
           (2, 'paid',    'us-east', 'apparel',      250.00, 101),
           (3, 'pending', 'us-west', 'electronics',  450.00, 102),
           (4, 'paid',    'us-east', 'electronics', 2200.00, 103),
           (5, 'paid',    'us-west', 'apparel',      150.00, 104)
       ) AS t(ORDER_ID, STATUS, REGION, CATEGORY, AMOUNT, CUSTOMER_ID);
  3. Run:
       uv run dg launch --assets '*'

What you'll see:
  - snowpark_pipeline opens a Session with the connection params above
  - Builds a 5-op LazyFrame plan against RAW.ORDERS
  - Compiles to ONE Snowflake SQL statement (filter → with_columns →
    group_by → sort → limit, all in one query plan)
  - Snowflake's optimizer runs it server-side; result lands as
    ANALYTICS.TOP_5_CATEGORIES_BY_REVENUE — no data through Python

Comparison with the warehouse_pipeline component:
  - warehouse_pipeline:  same idea but renders a CTAS with CTE-WITH chain
                         from a YAML op DSL — works on any SQLAlchemy-
                         supported warehouse (Snowflake / BigQuery / DuckDB /
                         Redshift / Databricks / Postgres / MSSQL / MySQL).
  - snowpark_pipeline:   uses Snowpark's native DataFrame API (richer
                         operators, e.g. join semantics, Snowflake-specific
                         functions). Snowflake-only.

Use snowpark_pipeline when you're Snowflake-native and want Snowpark's
full DataFrame DSL; use warehouse_pipeline when you want portability
across multiple warehouses.

Auth alternatives (key-pair / OAuth):
  - private_key_env_var + private_key_passphrase_env_var  (key-pair auth)
  - authenticator_env_var: externalbrowser                (SSO)
  - authenticator_env_var: oauth + token_env_var          (OAuth)
MSG
