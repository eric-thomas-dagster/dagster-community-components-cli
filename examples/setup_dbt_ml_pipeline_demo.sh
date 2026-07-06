#!/usr/bin/env bash
# setup_dbt_ml_pipeline_demo.sh
#
# THE flagship "why Dagster over Airflow" demo:
# **A Python ML model sits between two sets of dbt models, and Dagster
# stitches them into one lineage graph with zero glue code.**
#
# Shape:
#   staging (dbt) → customer_features → churn_prediction (Python) → churn_predictions → marts (dbt) → dim_customer_with_risk
#
# Airflow can't do this cleanly — each dbt run is one big opaque op. Dagster
# treats every dbt model as its own asset, so a Python asset drops in between
# and lineage flows correctly. That's the point of this demo.
#
# What it demonstrates
#   • DbtDocsEnrichedProjectComponent — dbt project as first-class Dagster assets
#   • ChurnPredictionComponent — heuristic churn scorer (no training data
#     needed, so no labels, so this demo is 100% self-contained)
#   • Cross-language lineage — dbt SQL ↔ Python ↔ dbt SQL all in one graph
#   • DuckDB as the shared storage layer (zero-cost, zero-config)
#
# Cost: $0. Everything local. No API keys, no external services.
#
# Requirements
#   • uv (https://docs.astral.sh/uv/)
#
# Usage
#   ./setup_dbt_ml_pipeline_demo.sh                      # → dbt_ml_demo/
#   ./setup_dbt_ml_pipeline_demo.sh my_pipeline          # custom name

set -eo pipefail

PROJECT_NAME="${1:-dbt_ml_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_YELLOW='\033[0;33m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

command -v uvx >/dev/null 2>&1 || fail "uvx not found. Install uv: https://docs.astral.sh/uv/"
[ -d "$PROJECT_DIR" ] && fail "Directory already exists: $PROJECT_DIR"

info "Target project: $PROJECT_DIR"

# ── Scaffold Dagster project ────────────────────────────────────────────────
info "Scaffolding Dagster project…"
uvx create-dagster project "$PROJECT_DIR" --uv-sync 2>&1 | tail -3 || fail "create-dagster failed"
cd "$PROJECT_DIR"

info "Installing deps (dagster-community-components + dagster-dbt + dbt-duckdb + sklearn)…"
if [ -n "${DCC_SRC:-}" ] && [ -d "$DCC_SRC" ]; then
  info "Using local DCC source: $DCC_SRC"
  uv add --quiet \
    "dagster-community-components @ ${DCC_SRC}" \
    'dagster-dbt>=0.24.0' 'dbt-core>=1.7.0' 'dbt-duckdb>=1.7.0' 'duckdb>=0.9.0' \
    'scikit-learn>=1.3.0' 'pandas>=1.5.0' \
    || fail "uv add failed"
else
  uv add --quiet \
    'dagster-community-components @ git+https://github.com/eric-thomas-dagster/dagster-component-templates.git' \
    'dagster-dbt>=0.24.0' 'dbt-core>=1.7.0' 'dbt-duckdb>=1.7.0' 'duckdb>=0.9.0' \
    'scikit-learn>=1.3.0' 'pandas>=1.5.0' \
    || fail "uv add failed"
fi
ok "Dependencies installed"

# ── Scaffold the dbt project ────────────────────────────────────────────────
mkdir -p dbt_analytics/{models/staging,models/marts,seeds}

DUCKDB_PATH="${PROJECT_DIR}/dbt_analytics/analytics.duckdb"

cat > dbt_analytics/dbt_project.yml <<YAML
name: 'dbt_analytics'
version: '1.0.0'
profile: 'dbt_analytics'
model-paths: ["models"]
seed-paths: ["seeds"]
target-path: "target"
clean-targets: ["target", "dbt_packages"]

# All models land in the default schema (main) so asset keys stay flat
# and cross-language references (Python asset ↔ dbt source) line up.
models:
  dbt_analytics:
    staging:
      +materialized: table
    marts:
      +materialized: table

seeds:
  dbt_analytics:
    +quote_columns: false
YAML

cat > dbt_analytics/profiles.yml <<YAML
dbt_analytics:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: analytics.duckdb
      threads: 4
YAML

# ── Seed CSVs ────────────────────────────────────────────────────────────────
cat > dbt_analytics/seeds/customers.csv <<'CSV'
customer_id,email,signup_date,plan
1,alice@example.com,2024-01-15,premium
2,bob@example.com,2024-02-20,basic
3,carol@example.com,2024-03-10,premium
4,dave@example.com,2024-01-05,basic
5,eve@example.com,2024-04-22,premium
6,frank@example.com,2024-05-01,basic
7,grace@example.com,2024-02-08,premium
8,heidi@example.com,2024-03-25,basic
9,ivan@example.com,2024-01-30,premium
10,judy@example.com,2024-06-14,basic
11,kevin@example.com,2024-07-01,basic
12,laura@example.com,2024-02-18,premium
13,mallory@example.com,2024-08-03,basic
14,nate@example.com,2024-03-15,premium
15,oscar@example.com,2024-04-10,basic
16,peggy@example.com,2024-05-22,premium
17,quinn@example.com,2024-06-05,basic
18,ruth@example.com,2024-07-18,premium
19,sam@example.com,2024-08-25,basic
20,tina@example.com,2024-09-02,premium
CSV

cat > dbt_analytics/seeds/orders.csv <<'CSV'
order_id,customer_id,order_date,amount
1001,1,2024-10-15,120.50
1002,1,2024-11-20,85.00
1003,1,2024-12-01,200.00
1004,1,2025-01-10,150.75
1005,2,2024-10-05,45.00
1006,2,2024-11-12,60.00
1007,3,2024-10-22,300.00
1008,3,2024-11-30,250.00
1009,3,2024-12-15,180.00
1010,3,2025-01-05,220.00
1011,4,2024-09-15,30.00
1012,5,2024-11-08,175.50
1013,5,2024-12-20,140.00
1014,5,2025-01-25,165.00
1015,6,2024-08-12,55.00
1016,7,2024-10-30,410.00
1017,7,2024-12-05,375.00
1018,7,2025-01-18,395.00
1019,8,2024-09-22,42.00
1020,9,2024-11-15,290.00
1021,9,2024-12-28,265.00
1022,10,2024-08-01,38.00
1023,12,2024-11-05,220.00
1024,12,2024-12-12,195.00
1025,12,2025-01-30,240.00
1026,14,2024-10-18,315.00
1027,14,2024-12-08,280.00
1028,16,2024-11-22,230.00
1029,16,2025-01-08,255.00
1030,18,2024-12-01,275.00
1031,18,2025-01-15,290.00
1032,20,2025-02-10,205.00
CSV

# ── dbt staging models ──────────────────────────────────────────────────────
cat > dbt_analytics/models/staging/stg_customers.sql <<'SQL'
{{ config(materialized='table') }}
select
  customer_id,
  email,
  signup_date,
  plan
from {{ ref('customers') }}
SQL

cat > dbt_analytics/models/staging/stg_orders.sql <<'SQL'
{{ config(materialized='table') }}
select
  order_id,
  customer_id,
  order_date,
  amount
from {{ ref('orders') }}
SQL

# The "input to the ML model" — customer-level features aggregated from orders.
cat > dbt_analytics/models/staging/customer_features.sql <<'SQL'
{{ config(materialized='table') }}
select
  c.customer_id,
  c.email,
  c.plan,
  c.signup_date,
  count(o.order_id)                                   as total_orders,
  coalesce(sum(o.amount), 0)                          as total_revenue,
  coalesce(avg(o.amount), 0)                          as avg_order_value,
  max(o.order_date)                                   as last_activity_date,
  cast(current_date - c.signup_date as integer)       as lifetime_days
from {{ ref('stg_customers') }} c
left join {{ ref('stg_orders') }} o using (customer_id)
group by 1, 2, 3, 4
SQL

# ── dbt marts model — reads the ML output as a SOURCE ───────────────────────
cat > dbt_analytics/models/marts/sources.yml <<'YAML'
version: 2
sources:
  - name: main
    schema: main
    tables:
      - name: churn_predictions
        description: "Churn risk scores from the Python asset between staging and marts"
YAML

# The "downstream of the ML model" — joins ChurnPredictionComponent's output
# (churn_risk_score / churn_risk_level / risk_factors / recommended_action)
# with the staging customer_features on the same customer_id.
cat > dbt_analytics/models/marts/dim_customer_with_risk.sql <<'SQL'
{{ config(materialized='table') }}
select
  f.customer_id,
  f.email,
  f.plan,
  f.total_orders,
  f.total_revenue,
  cp.churn_risk_score,
  cp.churn_risk_level,
  cp.activity_trend,
  cp.days_inactive,
  cp.risk_factors,
  cp.recommended_action                       as ml_recommended_action,
  case
    when f.plan = 'premium' and cp.churn_risk_level in ('Critical', 'High') then 'premium_immediate_outreach'
    when f.plan = 'premium' and cp.churn_risk_level = 'Medium' then 'premium_proactive_engagement'
    when f.plan = 'basic' and cp.churn_risk_level in ('Critical', 'High') then 'basic_upgrade_offer'
    when f.plan = 'basic' and cp.churn_risk_level = 'Medium' then 'basic_engagement_offer'
    else 'monitor'
  end                                         as business_action
from {{ ref('customer_features') }} f
left join {{ source('main', 'churn_predictions') }} cp on cp.customer_id = f.customer_id
order by cp.churn_risk_score desc nulls last
SQL

ok "Wrote dbt project (2 seeds + 3 staging models + 1 marts model)"

# ── Dagster defs.yaml files ────────────────────────────────────────────────
# TWO DbtProjectComponent instances on the same project, one per model dir,
# with EXPLICIT op names to avoid the collision that would otherwise happen
# from both defaulting to the dbt project's name. This lets Dagster run
# dbt-staging → Python asset → dbt-marts as three separate ops.
mkdir -p "src/${PROJECT_NAME}/defs/dbt_staging"
mkdir -p "src/${PROJECT_NAME}/defs/churn_model"
mkdir -p "src/${PROJECT_NAME}/defs/dbt_marts"

cat > "src/${PROJECT_NAME}/defs/dbt_staging/defs.yaml" <<YAML
type: dagster_dbt.DbtProjectComponent
attributes:
  project: "{{ project_root }}/dbt_analytics"
  select: "path:models/staging"
  op:
    name: dbt_staging_op
YAML

cat > "src/${PROJECT_NAME}/defs/dbt_marts/defs.yaml" <<YAML
type: dagster_dbt.DbtProjectComponent
attributes:
  project: "{{ project_root }}/dbt_analytics"
  select: "path:models/marts"
  op:
    name: dbt_marts_op
YAML

# The ML model between the dbt models. Reads customer_features (staging),
# writes churn_predictions (source for marts).
cat > "src/${PROJECT_NAME}/defs/churn_model/defs.yaml" <<YAML
type: dagster_community_components.ChurnPredictionComponent
attributes:
  asset_name: churn_predictions
  upstream_asset_key: customer_features
  inactivity_threshold_days: 90
  lookback_days: 180
  include_risk_factors: true
  # Auto-detects columns from customer_features:
  #   customer_id_field: customer_id  (already the default)
  #   last_activity_field: last_activity_date
  #   total_orders_field: total_orders
  #   total_revenue_field: total_revenue
  #   lifetime_days_field: lifetime_days
  description: "Heuristic churn risk scorer between staging + marts dbt models"
  group_name: ml
YAML

ok "Wrote 2 defs.yaml (dbt project component + churn Python component)"

# ── Wire the DuckDB IO manager so dbt + Python assets share the same DB ────
cat > "src/${PROJECT_NAME}/definitions.py" <<'PY'
from pathlib import Path
import os
from dagster import definitions, load_from_defs_folder
from dagster_duckdb_pandas import DuckDBPandasIOManager

@definitions
def defs():
    duckdb_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "..", "dbt_analytics", "analytics.duckdb",
    )
    duckdb_path = os.path.abspath(duckdb_path)
    return load_from_defs_folder(path_within_project=Path(__file__).parent).with_resources(
        resources={
            "io_manager": DuckDBPandasIOManager(
                database=duckdb_path,
                schema="main",
            ),
        }
    )
PY

info "Installing dagster-duckdb-pandas for cross-tool DuckDB IO manager…"
uv add --quiet 'dagster-duckdb-pandas>=0.24.0' 'dagster-duckdb>=0.24.0' || fail "uv add duckdb pandas io manager failed"

# ── Materialize the full pipeline ───────────────────────────────────────────
info "Running dbt seed (loads customers + orders CSVs into DuckDB)…"
(cd dbt_analytics && uv run dbt seed --profiles-dir .) 2>&1 | tail -8

info "Materializing the whole DAG (staging dbt → churn ML → marts dbt)…"
DM="${PROJECT_NAME}.definitions"
uv run dagster asset materialize --select '*' -m "$DM" 2>&1 | tail -20 || fail "materialize failed"

echo
ok "Demo complete."
echo
cat <<EOF
The pipeline just ran end-to-end:
  1. dbt seed loaded 20 customers + 32 orders into DuckDB
  2. dbt built stg_customers, stg_orders, customer_features (staging schema)
  3. Python asset (churn_prediction) read customer_features, computed
     churn_risk_score + risk_category per customer, wrote churn_predictions
  4. dbt built dim_customer_with_risk in the marts schema, joining
     customer_features with the ML output

Inspect the results:
  cd $PROJECT_NAME
  duckdb dbt_analytics/analytics.duckdb "SELECT * FROM main_marts.dim_customer_with_risk ORDER BY churn_risk_score DESC LIMIT 10"

Or open the Dagster UI to see the cross-language lineage graph:
  uv run dg dev
    → the asset graph shows dbt models (blue) → Python asset (yellow) → dbt models
    → each node is a first-class Dagster asset with lineage, materialization
      history, and metadata
    → this is exactly the shape Airflow can't do — dbt models are opaque
      DAG operators in Airflow, one Python step, run-and-pray

Next steps:
  • Swap ChurnPredictionComponent for LogisticRegressionModelComponent to
    train a real sklearn model (requires labeled data, so this heuristic
    scorer is what makes the demo zero-config)
  • Add a downstream reverse-ETL to Slack / HubSpot with the sinks in the
    registry (search for 'sink' in the UI)
  • Move DuckDB → Snowflake / BigQuery by swapping the IO manager
EOF
