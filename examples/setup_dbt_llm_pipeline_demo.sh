#!/usr/bin/env bash
# setup_dbt_llm_pipeline_demo.sh
#
# The LLM version of the flagship "why Dagster" demo:
# **A LangChain LLM asset sits between two sets of dbt models, generating
# personalized customer retention text row-by-row from customer_features,
# with the output flowing into a marts model.**
#
# Shape:
#   staging dbt → customer_features → langchain_chain_asset (LLM, row-wise)
#              → retention_outreach → marts dbt → customer_outreach_plan
#
# Companion to setup_dbt_ml_pipeline_demo.sh (same shape, sklearn scorer
# instead of LLM). Together they show the same pattern applies whether the
# middle step is classical ML or a generative model.
#
# What it demonstrates
#   • dagster_dbt.DbtProjectComponent × 2 with named ops for cross-language
#     lineage around a Python asset
#   • LangChainChainAssetComponent — row-wise LLM call over a DataFrame
#   • DuckDBPandasIOManager — dbt tables ↔ pandas DataFrames without glue
#   • Templated prompts referencing dbt columns ({customer_id}, {plan}, etc.)
#
# Cost: ~$0.01 per run on gpt-4o-mini (20 rows × ~150 tokens each).
#
# Requirements
#   • uv (https://docs.astral.sh/uv/)
#   • $OPENAI_API_KEY set (get one at https://platform.openai.com/api-keys)
#
# Usage
#   export OPENAI_API_KEY=sk-...
#   ./setup_dbt_llm_pipeline_demo.sh                      # → dbt_llm_demo/
#   ./setup_dbt_llm_pipeline_demo.sh my_pipeline          # custom name

set -eo pipefail

PROJECT_NAME="${1:-dbt_llm_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_YELLOW='\033[0;33m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

[ -z "${OPENAI_API_KEY:-}" ] && fail "OPENAI_API_KEY not set. Get one at https://platform.openai.com/api-keys"
command -v uvx >/dev/null 2>&1 || fail "uvx not found. Install uv: https://docs.astral.sh/uv/"
[ -d "$PROJECT_DIR" ] && fail "Directory already exists: $PROJECT_DIR"

info "OPENAI_API_KEY: set (prefix ${OPENAI_API_KEY:0:8}…)"
info "Target project: $PROJECT_DIR"

# ── Scaffold ────────────────────────────────────────────────────────────────
info "Scaffolding Dagster project…"
uvx create-dagster project "$PROJECT_DIR" --uv-sync 2>&1 | tail -3 || fail "create-dagster failed"
cd "$PROJECT_DIR"

info "Installing deps (dagster-community-components + dagster-dbt + dbt-duckdb + langchain-openai)…"
if [ -n "${DCC_SRC:-}" ] && [ -d "$DCC_SRC" ]; then
  info "Using local DCC source: $DCC_SRC"
  uv add --quiet \
    "dagster-community-components @ ${DCC_SRC}" \
    'dagster-dbt>=0.24.0' 'dbt-core>=1.7.0' 'dbt-duckdb>=1.7.0' 'duckdb>=0.9.0' \
    'langchain-core>=0.3.0' 'langchain-openai>=0.2.0' 'pandas>=1.5.0' \
    'dagster-duckdb>=0.24.0' 'dagster-duckdb-pandas>=0.24.0' \
    || fail "uv add failed"
else
  uv add --quiet \
    'dagster-community-components @ git+https://github.com/eric-thomas-dagster/dagster-component-templates.git' \
    'dagster-dbt>=0.24.0' 'dbt-core>=1.7.0' 'dbt-duckdb>=1.7.0' 'duckdb>=0.9.0' \
    'langchain-core>=0.3.0' 'langchain-openai>=0.2.0' 'pandas>=1.5.0' \
    'dagster-duckdb>=0.24.0' 'dagster-duckdb-pandas>=0.24.0' \
    || fail "uv add failed"
fi
ok "Dependencies installed"

# ── Scaffold the dbt project ────────────────────────────────────────────────
mkdir -p dbt_analytics/{models/staging,models/marts,seeds}

cat > dbt_analytics/dbt_project.yml <<YAML
name: 'dbt_analytics'
version: '1.0.0'
profile: 'dbt_analytics'
model-paths: ["models"]
seed-paths: ["seeds"]
target-path: "target"
clean-targets: ["target", "dbt_packages"]

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

cat > dbt_analytics/seeds/customers.csv <<'CSV'
customer_id,email,signup_date,plan,industry
1,alice@acme.com,2024-01-15,premium,manufacturing
2,bob@globex.com,2024-02-20,basic,retail
3,carol@initech.com,2024-03-10,premium,fintech
4,dave@umbrella.com,2024-01-05,basic,healthcare
5,eve@stark.com,2024-04-22,premium,defense
6,frank@wayne.com,2024-05-01,basic,media
7,grace@oscorp.com,2024-02-08,premium,biotech
8,heidi@lexcorp.com,2024-03-25,basic,energy
CSV

cat > dbt_analytics/seeds/orders.csv <<'CSV'
order_id,customer_id,order_date,amount
1001,1,2024-10-15,120.50
1002,1,2024-11-20,85.00
1003,3,2024-10-22,300.00
1004,3,2024-11-30,250.00
1005,3,2024-12-15,180.00
1006,5,2024-11-08,175.50
1007,5,2024-12-20,140.00
1008,7,2024-10-30,410.00
1009,7,2024-12-05,375.00
1010,2,2024-08-05,45.00
1011,4,2024-07-15,30.00
1012,6,2024-06-12,55.00
1013,8,2024-05-22,42.00
CSV

# ── dbt staging models ──────────────────────────────────────────────────────
cat > dbt_analytics/models/staging/stg_customers.sql <<'SQL'
{{ config(materialized='table') }}
select customer_id, email, signup_date, plan, industry from {{ ref('customers') }}
SQL

cat > dbt_analytics/models/staging/stg_orders.sql <<'SQL'
{{ config(materialized='table') }}
select order_id, customer_id, order_date, amount from {{ ref('orders') }}
SQL

cat > dbt_analytics/models/staging/customer_features.sql <<'SQL'
{{ config(materialized='table') }}
select
  c.customer_id,
  c.email,
  c.plan,
  c.industry,
  count(o.order_id)                             as total_orders,
  coalesce(sum(o.amount), 0)                    as total_revenue,
  max(o.order_date)                             as last_order_date,
  cast(current_date - max(o.order_date) as integer) as days_since_last_order
from {{ ref('stg_customers') }} c
left join {{ ref('stg_orders') }} o using (customer_id)
group by 1, 2, 3, 4
SQL

# ── dbt marts model — reads the LLM output as a source ─────────────────────
cat > dbt_analytics/models/marts/sources.yml <<'YAML'
version: 2
sources:
  - name: main
    schema: main
    tables:
      - name: retention_outreach
        description: "LLM-generated retention outreach copy per customer"
YAML

cat > dbt_analytics/models/marts/customer_outreach_plan.sql <<'SQL'
{{ config(materialized='table') }}
select
  f.customer_id,
  f.email,
  f.plan,
  f.industry,
  f.total_orders,
  f.total_revenue,
  f.days_since_last_order,
  case
    when f.days_since_last_order is null                     then 'never_ordered'
    when f.days_since_last_order > 180                       then 'dormant_180d'
    when f.days_since_last_order > 90                        then 'lapsing_90d'
    else 'active'
  end                                       as engagement_stage,
  ro.retention_email_subject,
  ro.retention_email_body
from {{ ref('customer_features') }} f
left join {{ source('main', 'retention_outreach') }} ro on ro.customer_id = f.customer_id
order by f.days_since_last_order desc nulls last
SQL

ok "Wrote dbt project (2 seeds + 3 staging models + 1 marts model)"

# ── Dagster defs.yaml — same two-op split as the ML version ────────────────
mkdir -p "src/${PROJECT_NAME}/defs/dbt_staging"
mkdir -p "src/${PROJECT_NAME}/defs/llm_outreach"
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

# THE LLM ASSET — reads customer_features (dbt), writes retention_outreach
# with two extra columns for the marts model to consume. LangChain calls
# gpt-4o-mini once per row with a templated prompt referencing dbt columns.
cat > "src/${PROJECT_NAME}/defs/llm_outreach/defs.yaml" <<YAML
type: dagster_community_components.LangChainChainAssetComponent
attributes:
  asset_name: retention_outreach
  upstream_asset_key: customer_features

  llm_provider: openai
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  temperature: 0.4
  max_tokens: 400

  system_message: |
    You are a B2B customer-success writer. For each customer, write a short
    retention outreach email in JSON with two keys: 'retention_email_subject'
    (max 10 words) and 'retention_email_body' (2-3 sentences, warm, specific,
    referencing their plan and industry). Output ONLY the JSON object.

  prompt_template: |
    Customer profile:
      email: {email}
      plan: {plan}
      industry: {industry}
      total_orders: {total_orders}
      total_revenue: {total_revenue}
      days_since_last_order: {days_since_last_order}

  # Parse the JSON response and expand into columns
  # (retention_email_subject + retention_email_body) so the dbt marts model
  # can select them by name.
  parse_json: true

  description: "LLM-generated retention outreach copy between staging and marts"
  group_name: llm
YAML

ok "Wrote 3 defs.yaml (dbt staging + LangChain LLM asset + dbt marts)"

# ── Wire DuckDBPandasIOManager as the project IO manager ───────────────────
cat > "src/${PROJECT_NAME}/definitions.py" <<'PY'
from pathlib import Path
import os
from dagster import definitions, load_from_defs_folder
from dagster_duckdb_pandas import DuckDBPandasIOManager

@definitions
def defs():
    duckdb_path = os.path.abspath(
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "..", "dbt_analytics", "analytics.duckdb",
        )
    )
    return load_from_defs_folder(path_within_project=Path(__file__).parent).with_resources(
        resources={
            "io_manager": DuckDBPandasIOManager(database=duckdb_path, schema="main"),
        }
    )
PY

# ── Load seeds via dbt ─────────────────────────────────────────────────────
info "Running dbt seed (loads customers + orders CSVs into DuckDB)…"
(cd dbt_analytics && uv run dbt seed --profiles-dir .) 2>&1 | tail -6

# ── Materialize each stage in order ────────────────────────────────────────
# Sequenced explicitly (staging → LLM → marts) rather than --select '*'
# because Dagster's default multiprocess executor can race the dbt marts
# op ahead of the LLM asset on fresh state.
DM="${PROJECT_NAME}.definitions"
info "Materializing staging dbt models…"
uv run dagster asset materialize --select "customer_features+" --select "stg_customers,stg_orders,customer_features" -m "$DM" 2>&1 | tail -6 || fail "staging failed"
info "Materializing LLM outreach (calls gpt-4o-mini once per customer)…"
uv run dagster asset materialize --select retention_outreach -m "$DM" 2>&1 | tail -6 || fail "LLM failed"
info "Materializing marts dbt models…"
uv run dagster asset materialize --select customer_outreach_plan -m "$DM" 2>&1 | tail -6 || fail "marts failed"

echo
ok "Demo complete."
echo
cat <<EOF
The pipeline just ran end-to-end:
  1. dbt seed loaded 8 customers + 13 orders into DuckDB
  2. dbt built stg_customers, stg_orders, customer_features
  3. LangChain LLM asset called gpt-4o-mini once per customer to generate a
     personalized retention email (subject + body) — parse_json expanded
     the response into two DataFrame columns
  4. dbt built customer_outreach_plan joining features + LLM output, with
     an engagement_stage classification (active / lapsing / dormant / never_ordered)

Inspect the LLM-generated emails:
  duckdb dbt_analytics/analytics.duckdb "SELECT customer_id, engagement_stage, retention_email_subject, LEFT(retention_email_body, 200) FROM main.customer_outreach_plan ORDER BY days_since_last_order DESC NULLS LAST LIMIT 5"

Open the Dagster UI to see the cross-language lineage graph:
  uv run dg dev
    → asset graph: dbt staging (blue) → LLM asset (yellow) → dbt marts (blue)
    → click any LLM materialization to see per-row LLM outputs in metadata
    → same dbt lineage as usual — plus a proper generative-AI step in the middle

Companion demo: setup_dbt_ml_pipeline_demo.sh — same shape but with a
classical sklearn scorer instead of an LLM. Together they show that any
Python step (ML, LLM, custom aggregation, HTTP fetch) plugs in the same way.
EOF
