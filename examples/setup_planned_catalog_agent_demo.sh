#!/usr/bin/env bash
# setup_planned_catalog_agent_demo.sh
#
# Planned Catalog Agent — the `dg.StateBackedComponent` variant of
# catalog_agent. LLM planner + real component materializations run ONCE
# at prepare time (`write_state_to_path`) and the full plan is cached
# to Dagster's native state store. Every subsequent load reads the
# cached plan and emits REAL Dagster assets — zero LLM cost.
#
# vs catalog_agent:
#   • catalog_agent    — LLM plans per-step every run, emits step_N wrappers
#   • planned_catalog_agent — LLM plans ONCE, emits REAL component assets,
#                             refreshed only on explicit `dg utils refresh-defs-state`
#
# COST: ~$0.02 for the ONE trajectory. Every subsequent materialization
#       is free — no LLM. Ship to prod, run daily, pay once.
#
# REQUIREMENTS
#   • uv, OPENAI_API_KEY, internet (fetches manifest.json)
#
# USAGE
#   export OPENAI_API_KEY=sk-...
#   ./setup_planned_catalog_agent_demo.sh          # → planned_catalog_agent_demo/

set -eo pipefail

PROJECT_NAME="${1:-planned_catalog_agent_demo}"
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

# === defs.yaml: state-backed planner cached to disk =====================
mkdir -p "src/${PROJECT_NAME}/defs/planned"
cat > "src/${PROJECT_NAME}/defs/planned/defs.yaml" <<'YAML'
type: dagster_community_components.PlannedCatalogAgentComponent
attributes:
  # Same natural-language task as catalog_agent's join demo — no explicit
  # steps, no field-name hints. The planner has to figure out the pipeline
  # shape (gen sources → join → derive month → aggregate → CSV) from
  # intent alone. But this happens ONCE, at `write_state_to_path` time.
  task: |
    Generate synthetic orders and synthetic customers, join them,
    group by first name, email, and month, sum total and count of
    orders, and store to a csv at /tmp/planned_orders_by_customer_month.csv.
  # Same additive filter as catalog_agent — union of category + specific ids.
  include_categories: [source, ingestion, transformation, sink]
  include_ids: [synthetic_data_generator]
  llm_model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  max_iterations: 8
  group_name: planned_agent_demo
  # State-backed knobs. `refresh_if_dev: false` means the planner does NOT
  # re-run on every `dg` invocation — only when we explicitly refresh via
  # `dg utils refresh-defs-state`. This is the "input once, run forever"
  # UX: change the task or run refresh to re-plan; otherwise cache is the
  # source of truth and materialization is pure Dagster (no LLM).
  defs_state:
    management_type: LOCAL_FILESYSTEM
    refresh_if_dev: false
YAML

ok "Wrote defs.yaml"

DM="${PROJECT_NAME}.definitions"

info "Refreshing defs state — runs the LLM planner trajectory once…"
uv run dg utils refresh-defs-state 2>&1 | tail -5 || fail "refresh-defs-state failed"
ok "Plan cached — subsequent loads are pure cache reads (no LLM cost)"

STATE_FILE="$(find src/${PROJECT_NAME}/defs/.local_defs_state -name 'state' -type f 2>/dev/null | head -1)"
if [ -n "$STATE_FILE" ] && [ -f "$STATE_FILE" ]; then
  info "Cached plan (first 5 picks):"
  uv run python -c "
import json
d = json.load(open('$STATE_FILE'))
plan = d.get('plan', [])
print(f'  {len(plan)} picks planned, {sum(1 for p in plan if p.get(\"status\") == \"success\") } succeeded')
for i, p in enumerate(plan[:5], 1):
    print(f'  {i}. {p.get(\"asset_name\")} ({p.get(\"component_type\",\"?\").split(\".\")[-1]}) — {p.get(\"status\")}')
"
fi

info "Materializing all cached-plan assets (zero LLM calls)…"
uv run dagster asset materialize --select '*' -m "$DM" 2>&1 | tail -3 || fail "materialize failed"

echo
ok "Demo complete."
echo
cat <<EOF
The Dagster+ UI story this demonstrates:

  1. User creates a new defs.yaml in the UI with a natural-language task
  2. The code server runs \`write_state_to_path\` — planner + real
     materializations run ONCE, cached to local_defs_state/
  3. REAL component assets appear in the graph — no step_N wrappers,
     just the concrete assets the planner picked
  4. User materializes; NO LLM runs — pure cached-plan execution

Inspect the cache manually:
  cat src/${PROJECT_NAME}/defs/.local_defs_state/*/state | jq '.plan[] | {asset_name, component_type, status}'

Re-plan (edit the task, then):
  uv run dg utils refresh-defs-state

Open the UI:
  cd ${PROJECT_NAME}
  uv run dg dev
    → asset graph shows the concrete pipeline the planner built
    → no LLM cost per run — the plan is frozen until you refresh state
EOF
