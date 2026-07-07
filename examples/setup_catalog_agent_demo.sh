#!/usr/bin/env bash
# setup_catalog_agent_demo.sh
#
# Catalog Agent — the most sophisticated agentic primitive.
# Per-step planner picks REAL components from the LIVE 900-component
# manifest. At each step, the planner sees the ACTUAL columns from the
# prior step's real materialized output — so the agent handles data with
# schemas unknown at pipeline-write time.
#
# How this differs from iterative_supervisor_agent:
#   • Supervisor tools are LLM personas (hand-authored, roleplay).
#   • Catalog Agent tools are REAL Dagster components from the 900-
#     component manifest, executed via reflection + in-process materialize.
#
# Pipeline:
#   catalog_step_1..N   (per-step: fetch catalog → planner sees prior
#                        outputs' REAL columns → picks component → executes
#                        for real → captures columns for next step)
#         ↓
#   catalog_final_answer (synthesizer LLM reads full trajectory)
#
# COST: ~$0.02 per run (N planner calls + 1 synthesis on gpt-4o-mini).
# Real component execution is free — just Dagster running assets.
#
# REQUIREMENTS
#   • uv, OPENAI_API_KEY, internet (fetches manifest.json)
#
# USAGE
#   export OPENAI_API_KEY=sk-...
#   ./setup_catalog_agent_demo.sh          # → catalog_agent_demo/

set -eo pipefail

PROJECT_NAME="${1:-catalog_agent_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

[ -z "${OPENAI_API_KEY:-}" ] && fail "OPENAI_API_KEY not set."
command -v uvx >/dev/null 2>&1 || fail "uvx not found."
[ -d "$PROJECT_DIR" ] && fail "Directory exists: $PROJECT_DIR"

info "Target project: $PROJECT_DIR"
info "Scaffolding Dagster project…"
uvx create-dagster project "$PROJECT_DIR" --uv-sync 2>&1 | tail -3 || fail "create-dagster failed"
cd "$PROJECT_DIR"

info "Installing deps…"
if [ -n "${DCC_SRC:-}" ] && [ -d "$DCC_SRC" ]; then
  info "Using local DCC source: $DCC_SRC"
  uv add --quiet "dagster-community-components @ ${DCC_SRC}" \
    'pandas>=1.5.0' 'tabulate>=0.9.0' 'openai>=1.0.0' \
    || fail "uv add failed"
else
  uv add --quiet 'dagster-community-components @ git+https://github.com/eric-thomas-dagster/dagster-component-templates.git' \
    'pandas>=1.5.0' 'tabulate>=0.9.0' 'openai>=1.0.0' \
    || fail "uv add failed"
fi
ok "Deps installed"

mkdir -p "$PROJECT_DIR/.dagster_storage"
cat > "src/${PROJECT_NAME}/definitions.py" <<'PY'
from pathlib import Path
from dagster import definitions, load_from_defs_folder, FilesystemIOManager

@definitions
def defs():
    root = Path(__file__).resolve().parent.parent.parent
    storage = root / ".dagster_storage"; storage.mkdir(exist_ok=True)
    return load_from_defs_folder(path_within_project=Path(__file__).parent).with_resources(
        resources={"io_manager": FilesystemIOManager(base_dir=str(storage))},
    )
PY

# === DEMO 1: simple schema-discovery pipeline (single source) ============
mkdir -p "src/${PROJECT_NAME}/defs/simple_agent"
cat > "src/${PROJECT_NAME}/defs/simple_agent/defs.yaml" <<'YAML'
type: dagster_community_components.CatalogAgentComponent
attributes:
  step_asset_prefix: simple_step
  synthesis_asset_name: simple_final_answer
  # Task deliberately does NOT tell the agent what columns exist —
  # it must discover them from step 1's real output.
  task: |
    Do a small analytics workflow:
    Step A: Generate 100 rows of synthetic data. Pick any interesting
            schema type from the available options (customers, orders,
            transactions, support_tickets, etc.).
    Step B: Once you see the actual columns produced, filter the data
            to some interesting subset using real column names you now
            know exist.
    Step C: Summarize the filtered set — pick reasonable group_by and
            aggregation columns from what actually exists.
    Then declare done. Chain each step using upstream_asset_key.
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  # Filters are ADDITIVE (OR). Agent sees the union of:
  #   • all source / ingestion / transformation / sink components
  #   • plus synthetic_data_generator (which lives under `ai`)
  # No cherry-picked list of every component the agent needs — just
  # the scope (categories) + the outlier we want included by name.
  include_categories: [source, ingestion, transformation, sink]
  include_ids: [synthetic_data_generator]
  max_catalog_entries: 300
  max_iterations: 5
  group_name: catalog_simple_demo
YAML

# === DEMO 2: multi-source join pipeline (two sources → join → aggregate → CSV) ===
mkdir -p "src/${PROJECT_NAME}/defs/join_agent"
cat > "src/${PROJECT_NAME}/defs/join_agent/defs.yaml" <<'YAML'
type: dagster_community_components.CatalogAgentComponent
attributes:
  step_asset_prefix: join_step
  synthesis_asset_name: join_final_answer
  # Deliberately vague / user-natural phrasing — no explicit steps,
  # no field-name hints, no wiring instructions. The agent has to
  # figure out the pipeline shape (gen sources → join → derive
  # month → aggregate → CSV) from the intent alone.
  task: |
    Generate synthetic orders and synthetic customers, join them,
    group by first name, email, and month, sum total and count of
    orders, and store to a csv at /tmp/orders_by_customer_month.csv.
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  # Filters are ADDITIVE (OR). Agent sees the union of:
  #   • all source / ingestion / transformation / sink components
  #   • plus synthetic_data_generator (which lives under `ai`)
  # The prompt is compressed — we skip common infra fields (partition_*,
  # freshness_*, retry_policy_*, owners, tags, etc.) since the planner
  # rarely needs them for a first-pass pick.
  include_categories: [source, ingestion, transformation, sink]
  include_ids: [synthetic_data_generator]
  max_catalog_entries: 300
  max_iterations: 8
  group_name: catalog_join_demo
YAML

ok "Wrote 2 defs.yaml files (simple + join)"

DM="${PROJECT_NAME}.definitions"

info "Running DEMO 1 — simple schema-discovery pipeline…"
uv run dagster asset materialize --select 'simple_step_1+' -m "$DM" 2>&1 | tail -3 || fail "simple demo failed"

info "Running DEMO 2 — multi-source join pipeline…"
uv run dagster asset materialize --select 'join_step_1+' -m "$DM" 2>&1 | tail -3 || fail "join demo failed"

echo
ok "Both demos complete."
echo
cat <<EOF
DEMO 1 (simple_step_* + simple_final_answer): schema discovery.
  1. Planner picked a schema_type (customers/orders/etc.)
  2. Executor materialized synthetic_data — real columns discovered
  3. Planner filtered using a real column name from step 1
  4. Planner summarized using real column names from step 2
  5. Declared done; later steps short-circuit

DEMO 2 (join_step_* + join_final_answer): multi-source join with self-correction.
  If the planner references an upstream that doesn't exist yet (e.g. tries
  to join orders before generating them), the validator catches it,
  surfaces the error to the NEXT step's planner, and the planner
  course-corrects. Typical successful trajectory:
    step 1: generate customers  ← or orders (agent picks either first)
    step 2: dataframe_join      ← may fail if planner forward-references
    step 3: generate the OTHER source  ← course-correct!
    step 4: dataframe_join      ← now succeeds with BOTH upstreams wired
    step 5: formula (month from order_date)
    step 6: summarize by (first_name, email, month)
    step 7: dataframe_to_csv → /tmp/orders_by_customer_month.csv
    step 8: DONE

Inspect the CSV DEMO 2 produced:
  cat /tmp/orders_by_customer_month.csv

Inspect both trajectories:
  cd $PROJECT_NAME
  uv run dg dev
    → asset graph shows BOTH pipelines side by side
    → simple_step_1..5 + simple_final_answer  (schema-discovery demo)
    → join_step_1..8 + join_final_answer      (multi-source join demo)
    → click any step to see the planner's pick + reason + REAL output columns

This works for customer-built data because the planner learns the schema
FROM the actual materialized output. Point step 1 at a Snowflake table,
S3 CSV, or any real DataFrame source — the agent discovers columns and
plans the rest of the pipeline accordingly.
EOF
