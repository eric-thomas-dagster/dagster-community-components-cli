#!/usr/bin/env bash
# customer_360 — a "typical Dagster" demo: multi-vendor ingest → dbt → dynamic
# fan-out. One code location. Local DuckDB. No Docker. 100% components + YAML.
#
# What's in it:
#   1. INGEST (3 "vendors" via SyntheticDataGeneratorComponent):
#        - customers        (dim table — 500 customers)
#        - orders           (Shopify-ish orders, ~3000 rows)
#        - stripe_charges   (Stripe payments, ~3000 rows)
#      Each landed in DuckDB via DuckDBTableWriterComponent as raw_*.
#
#   2. TRANSFORM (real dbt via DbtDocsEnrichedProjectComponent):
#        - stg_customers, stg_orders, stg_charges  (staging cleanups)
#        - fct_customer_daily                       (daily rollup per customer)
#      Materialized in the same DuckDB. Assets appear in the Dagster graph
#      with real column-level lineage (dbt docs enrichment).
#
#   3. FAN-OUT (DynamicFanoutAssetComponent):
#      Reads fct_customer_daily; discover() emits 4 cohorts based on
#      spend/activity; process() filters + ranks customers per cohort;
#      collect() writes per-cohort CSV extracts for the marketing team.

set -eo pipefail

PROJECT_DIR="${1:-customer-360-demo}"
COMMIT_SHA="${COMMIT_SHA:-main}"

if ! command -v uv >/dev/null 2>&1; then echo "✗ uv required"; exit 1; fi

if [ -n "$DCC_LOCAL_PATH" ]; then
  DCC_SRC="dagster-community-components @ file://$DCC_LOCAL_PATH"
else
  DCC_SRC="dagster-community-components @ https://github.com/eric-thomas-dagster/dagster-component-templates/archive/$COMMIT_SHA.zip"
fi

rm -rf "$PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync 2>&1 | tail -3
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"

# --- Deps -----------------------------------------------------------------
uv add -q "$DCC_SRC" pandas duckdb dagster-duckdb dagster-dbt dbt-core dbt-duckdb

export DAGSTER_HOME="$PROJECT_ABS/.dagster_home"
mkdir -p "$DAGSTER_HOME" data extracts

# --- Create the dbt project (3 staging + 1 fact) --------------------------
mkdir -p dbt_project/models/staging dbt_project/models/marts
DBT_DB_PATH="$PROJECT_ABS/data/warehouse.duckdb"

cat > dbt_project/dbt_project.yml <<YAML
name: customer_360
version: '1.0.0'
config-version: 2
profile: customer_360
model-paths: ["models"]
target-path: "target"
clean-targets: ["target", "dbt_packages"]
models:
  customer_360:
    +materialized: table
YAML

cat > dbt_project/profiles.yml <<YAML
customer_360:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: ${DBT_DB_PATH}
      threads: 4
YAML

# Sources — reference the DuckDB tables Dagster's ingest layer creates
cat > dbt_project/models/staging/_sources.yml <<'YAML'
version: 2
sources:
  - name: raw
    schema: main
    tables:
      - name: raw_customers
      - name: raw_orders
      - name: raw_stripe_charges
YAML

# Staging models — trivial column renames + type nudges
cat > dbt_project/models/staging/stg_customers.sql <<'SQL'
SELECT
  customer_id,
  first_name,
  last_name,
  email,
  city
FROM {{ source('raw', 'raw_customers') }}
SQL

cat > dbt_project/models/staging/stg_orders.sql <<'SQL'
SELECT
  order_id,
  customer_id,
  CAST(order_date AS DATE) AS order_date,
  total AS order_total,
  num_items
FROM {{ source('raw', 'raw_orders') }}
SQL

cat > dbt_project/models/staging/stg_charges.sql <<'SQL'
SELECT
  customer_id,
  CAST(TO_TIMESTAMP(created) AS DATE)         AS charge_date,
  CAST(amount / 100.0 AS DECIMAL(18,2))       AS charge_amount_usd,
  status
FROM {{ source('raw', 'raw_stripe_charges') }}
WHERE status IN ('succeeded', 'paid')
SQL

# Mart — the join across all 3 vendors, per-customer per-day
cat > dbt_project/models/marts/fct_customer_daily.sql <<'SQL'
WITH order_totals AS (
  SELECT customer_id, order_date AS activity_date,
         SUM(order_total) AS orders_total,
         COUNT(*)         AS orders_count
  FROM {{ ref('stg_orders') }}
  GROUP BY 1, 2
),
charge_totals AS (
  SELECT customer_id, charge_date AS activity_date,
         SUM(charge_amount_usd) AS charges_total,
         COUNT(*)               AS charges_count
  FROM {{ ref('stg_charges') }}
  GROUP BY 1, 2
),
merged AS (
  SELECT
    COALESCE(o.customer_id, c.customer_id) AS customer_id,
    COALESCE(o.activity_date, c.activity_date) AS activity_date,
    COALESCE(o.orders_total, 0)    AS orders_total,
    COALESCE(o.orders_count, 0)    AS orders_count,
    COALESCE(c.charges_total, 0)   AS charges_total,
    COALESCE(c.charges_count, 0)   AS charges_count
  FROM order_totals o
  FULL OUTER JOIN charge_totals c USING (customer_id, activity_date)
)
SELECT
  cust.customer_id,
  cust.first_name || ' ' || cust.last_name AS customer_name,
  cust.email,
  cust.city,
  m.activity_date,
  m.orders_total,
  m.orders_count,
  m.charges_total,
  m.charges_count,
  m.orders_total + m.charges_total AS total_activity_value
FROM merged m
LEFT JOIN {{ ref('stg_customers') }} cust USING (customer_id)
SQL

# --- Ingest layer defs.yaml files ----------------------------------------
PKG="$(ls src/ | head -1)"
DEFS="src/$PKG/defs"

# 1. DuckDB resource (shared by ingest sinks + dbt project)
mkdir -p "$DEFS/warehouse"
cat > "$DEFS/warehouse/defs.yaml" <<YAML
type: dagster_community_components.DuckDBResourceComponent
attributes:
  resource_key: warehouse
  database: ${DBT_DB_PATH}
YAML

# 2. Three synthetic vendor sources
for VENDOR in customers orders stripe_charges; do
  case "$VENDOR" in
    customers) ROW_COUNT=500 ;;
    orders) ROW_COUNT=3000 ;;
    stripe_charges) ROW_COUNT=3000 ;;
  esac
  mkdir -p "$DEFS/src_${VENDOR}"
  cat > "$DEFS/src_${VENDOR}/defs.yaml" <<YAML
type: dagster_community_components.SyntheticDataGeneratorComponent
attributes:
  asset_name: src_${VENDOR}
  schema_type: ${VENDOR}
  row_count: ${ROW_COUNT}
  random_state: 42
  group_name: vendor_sources
YAML

  # Corresponding DuckDB sink into raw schema
  mkdir -p "$DEFS/raw_${VENDOR}"
  cat > "$DEFS/raw_${VENDOR}/defs.yaml" <<YAML
type: dagster_community_components.DuckDBTableWriterComponent
attributes:
  asset_name: raw_${VENDOR}
  upstream_asset_key: src_${VENDOR}
  database_path: ${DBT_DB_PATH}
  table: raw_${VENDOR}
  write_mode: replace
  group_name: raw_warehouse
YAML
done

# 3. dbt project component (dagster_dbt.DbtProjectComponent — the standard one)
mkdir -p "$DEFS/dbt"
cat > "$DEFS/dbt/defs.yaml" <<YAML
type: dagster_dbt.DbtProjectComponent
attributes:
  project: ${PROJECT_ABS}/dbt_project
YAML

# 4. Helpers module for the fanout callables
cat > "src/$PKG/helpers.py" <<PY
"""Callables for the DynamicFanoutAssetComponent — per-cohort processing."""
import json
from pathlib import Path
from typing import Any, Dict, List

import duckdb
import pandas as pd

# Cohort definitions — thresholds on total_activity_value (orders + charges)
COHORTS = [
    {"cohort": "high_value",  "min_value": 500, "max_value": None, "priority": 1},
    {"cohort": "medium",      "min_value": 100, "max_value": 500,  "priority": 2},
    {"cohort": "low",         "min_value": 1,   "max_value": 100,  "priority": 3},
    {"cohort": "at_risk",     "min_value": 0,   "max_value": 1,    "priority": 4},
]


def discover_cohorts(**kwargs) -> List[Dict[str, Any]]:
    """Emit one item per cohort. Each becomes a DynamicOutput → process call."""
    return COHORTS


def rank_cohort(cohort: Dict[str, Any], *, db_path: str, extracts_dir: str) -> Dict[str, Any]:
    """For one cohort, query fct_customer_daily and rank customers by
    total_activity_value. Write a per-cohort CSV extract."""
    con = duckdb.connect(db_path, read_only=True)
    where = "total_activity_value >= ?"
    params = [cohort["min_value"]]
    if cohort["max_value"] is not None:
        where += " AND total_activity_value < ?"
        params.append(cohort["max_value"])
    rows = con.execute(f"""
        SELECT customer_id, customer_name, email, city,
               SUM(total_activity_value) AS lifetime_value,
               SUM(orders_count) AS orders_count,
               SUM(charges_count) AS charges_count,
               MAX(activity_date) AS last_activity
        FROM fct_customer_daily
        WHERE {where}
        GROUP BY 1,2,3,4
        ORDER BY lifetime_value DESC
        LIMIT 500
    """, params).fetchdf()
    con.close()

    Path(extracts_dir).mkdir(parents=True, exist_ok=True)
    out_path = Path(extracts_dir) / f"{cohort['cohort']}_customers.csv"
    rows.to_csv(out_path, index=False)
    return {
        "cohort": cohort["cohort"],
        "priority": cohort["priority"],
        "n_customers": len(rows),
        "top_lifetime_value": float(rows["lifetime_value"].max()) if len(rows) else 0.0,
        "extract_path": str(out_path),
    }


def summarize_batch(results: List[Dict[str, Any]]) -> pd.DataFrame:
    """Aggregate per-cohort results into a summary DataFrame."""
    df = pd.DataFrame(results)
    if not df.empty:
        df = df.sort_values("priority").reset_index(drop=True)
    return df


# Wrapper because DynamicFanoutAssetComponent's collect_callable only takes
# (results,) — bind the paths at import time.
_DB_PATH = "${DBT_DB_PATH}"
_EXTRACTS_DIR = "${PROJECT_ABS}/extracts"

def rank_cohort_default(cohort):
    return rank_cohort(cohort, db_path=_DB_PATH, extracts_dir=_EXTRACTS_DIR)

def summarize_batch_default(results):
    return summarize_batch(results)
PY

# 5. Dynamic fan-out over the 4 cohorts
mkdir -p "$DEFS/cohort_extracts"
cat > "$DEFS/cohort_extracts/defs.yaml" <<YAML
type: dagster_community_components.DynamicFanoutAssetComponent
attributes:
  asset_name: cohort_extracts
  group_name: cohort_extracts
  description: "Per-cohort customer priority extracts. Fans out from fct_customer_daily."
  discover_callable_path: "${PKG}.helpers:discover_cohorts"
  process_callable_path: "${PKG}.helpers:rank_cohort_default"
  collect_callable_path: "${PKG}.helpers:summarize_batch_default"
  mapping_key_field: cohort
  retry_max_retries: 1
  retry_delay_seconds: 2
  kinds: [csv, cohort]
YAML

# --- dg check + materialize -----------------------------------------------
echo ""
echo ">>> dg check defs"
if ! uv run dg check defs 2>&1 | tail -8; then
  echo "    ✗ dg check failed"; exit 1
fi

echo ""
echo ">>> Ingest layer — 3 synthetic vendors + raw DuckDB landings (sequential — DuckDB doesn't like concurrent writes)"
uv run dg launch --assets 'src_customers,src_orders,src_stripe_charges' 2>&1 | tail -2
for T in customers orders stripe_charges; do
  uv run dg launch --assets "raw_$T" 2>&1 | tail -1
done

echo ""
echo ">>> dbt build — 3 staging + 1 fact model"
uv run dg launch --assets 'stg_customers,stg_orders,stg_charges,fct_customer_daily' 2>&1 | tail -8

echo ""
echo ">>> Dynamic fan-out over 4 customer cohorts"
uv run dg launch --assets cohort_extracts 2>&1 | tail -3

echo ""
echo ">>> Cohort extracts on disk:"
ls -1 "$PROJECT_ABS/extracts/" 2>&1 | sed 's/^/    /' || echo "  (empty)"

echo ""
echo ">>> Warehouse contents summary:"
uv run python - <<PY
import duckdb
con = duckdb.connect("$DBT_DB_PATH", read_only=True)
print("  Raw tables:")
for t in ("raw_customers","raw_orders","raw_stripe_charges"):
    try:
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"    {t:35s} {n:>7d} rows")
    except Exception as e:
        print(f"    {t}: (missing — {e})")
print("  dbt marts:")
try:
    rows = con.execute("SELECT COUNT(*) AS n, ROUND(SUM(total_activity_value),0) AS gmv FROM fct_customer_daily").fetchone()
    print(f"    fct_customer_daily                  {rows[0]:>7d} rows  (GMV \\\${rows[1]:,.0f})")
except Exception as e:
    print(f"    fct_customer_daily                  (missing — {e})")
con.close()
PY

# --- Dagster+ Serverless prep -------------------------------------------
# Make the project deployable via `dagster-cloud serverless deploy-docker`.
# Adds dagster-cloud + boto3 deps, emits dagster_cloud_post_install.sh, and
# rewrites definitions.py with a conditional serverless_io_manager (leaves
# local `dg dev` behavior unchanged).
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/lib/serverless_prep.sh | bash

cat <<DONE

✓ customer_360 demo done.

Multi-vendor customer 360, all in one Dagster project. Real dbt. Real
DuckDB. Local disk. No Docker.

The asset graph (browse at http://localhost:3000):

  ┌─── vendor_sources ──────┐    ┌─── raw_warehouse ──────────┐    ┌─── dbt ─────────────────────┐    ┌─── cohort_extracts ───────┐
  │ src_customers           │───►│ raw_customers              │───►│ stg_customers               │───►│ cohort_extracts           │
  │ src_orders              │───►│ raw_orders                 │───►│ stg_orders     ─┐           │    │ (dynamic fan-out over     │
  │ src_stripe_charges      │───►│ raw_stripe_charges         │───►│ stg_charges    ─┤           │    │  4 cohorts → 4 CSVs)      │
  └─────────────────────────┘    └────────────────────────────┘    │ fct_customer_daily ◄──┘     │    └───────────────────────────┘
                                                                    └─────────────────────────────┘

Browse:
  cd $PROJECT_ABS
  uv run dg dev
  # http://localhost:3000
  # Assets tab → click any node → lineage, materialization history, metadata
  # dbt models auto-load with full lineage via dagster_dbt.DbtProjectComponent

Cleanup:
  rm -rf $PROJECT_ABS
DONE
