#!/usr/bin/env bash
# setup_iterative_catalog_agent_demo.sh
#
# Iterative Catalog Agent — the most sophisticated agentic primitive.
# Fusion of iterative-step-per-materialization + live-manifest catalog.
# At each step, the planner sees the ACTUAL columns from the prior step's
# real materialized output — so the agent handles data with unknown schemas.
#
# What's different from component_catalog_agent:
#   • Single-shot catalog: planner picks all steps upfront (no schema info
#     for downstream picks — has to guess column names).
#   • Iterative catalog (this): planner picks step 1, executes, sees REAL
#     columns, then picks step 2 with knowledge of the real schema.
#
# What's different from iterative_supervisor_agent:
#   • Supervisor tools are LLM personas (hand-authored, roleplay).
#   • Iterative catalog tools are REAL Dagster components from the 900-
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
#   ./setup_iterative_catalog_agent_demo.sh          # → iterative_catalog_agent_demo/

set -eo pipefail

PROJECT_NAME="${1:-iterative_catalog_agent_demo}"
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

mkdir -p "src/${PROJECT_NAME}/defs/agent"

cat > "src/${PROJECT_NAME}/defs/agent/defs.yaml" <<'YAML'
type: dagster_community_components.IterativeCatalogAgentComponent
attributes:
  step_asset_prefix: catalog_step
  synthesis_asset_name: catalog_final_answer
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
  include_ids:
    - synthetic_data_generator
    - filter
    - summarize
    - dataframe_describe
  max_iterations: 5
  group_name: iterative_catalog_demo
YAML

ok "Wrote defs.yaml"

DM="${PROJECT_NAME}.definitions"
info "Running iterative catalog agent (5 steps + synthesis)…"
uv run dagster asset materialize --select '*' -m "$DM" 2>&1 | tail -3 || fail "run failed"

echo
ok "Demo complete."
echo
cat <<EOF
The iterative catalog agent just ran:
  1. Step 1: planner fetched the manifest, filtered to your include_ids,
     picked ONE real component + config. Executor materialized it in-process.
  2. Step 2: planner saw step 1's REAL output columns + preview, then picked
     the next component with knowledge of the actual schema. Executor
     ran it, wiring the upstream via a source asset seeded with step 1's
     DataFrame.
  3. Continued until planner declared done. Later steps short-circuit.
  4. Synthesizer wrote the final answer citing each step.

Inspect the trajectory:
  cd $PROJECT_NAME
  uv run dg dev
    → asset graph: catalog_step_1 → catalog_step_2 → … → catalog_final_answer
    → click each step to see the planner's pick + reason + REAL output columns
    → click catalog_final_answer to see the synthesized description

This works for customer-built data because the planner learns the schema
FROM the actual materialized output. Point step 1 at a Snowflake table,
S3 CSV, or any DataFrame source — the agent discovers the columns and
plans the rest of the pipeline accordingly.
EOF
