#!/usr/bin/env bash
# setup_component_catalog_agent_demo.sh
#
# Component Catalog Agent — the agent picks from the LIVE registry (900+
# components) and actually invokes the real components via reflection +
# in-process materialize. Not simulated. Not curated. The catalog IS
# the manifest.
#
# How it works:
#   1. At runtime, component fetches manifest.json from the registry
#   2. Filter it (by category, tags, or a list of IDs) → bounded action space
#   3. Pydantic model_fields inspected to give planner the exact field names
#   4. Planner LLM sees the filtered catalog + fields → picks components + configs
#   5. Executor imports each picked class by name → instantiates → build_defs()
#      → dg.materialize() runs it in-process against real data
#   6. Synthesizer LLM writes final answer citing the real component outputs
#
# Pipeline (3 assets):
#   catalog_plan       (planner picks {component_id, component_type, config, reason})
#         ↓
#   catalog_execution  (reflection-instantiate + REAL dg.materialize per pick)
#         ↓
#   catalog_answer     (synthesizer LLM writes grounded final answer)
#
# COST: ~$0.02 per run (planner + synthesis on gpt-4o-mini). Real
# component execution has zero LLM cost — it's just Dagster running assets.
#
# REQUIREMENTS
#   • uv, OPENAI_API_KEY, internet (fetches manifest.json)
#
# USAGE
#   export OPENAI_API_KEY=sk-...
#   ./setup_component_catalog_agent_demo.sh          # → component_catalog_agent_demo/

set -eo pipefail

PROJECT_NAME="${1:-component_catalog_agent_demo}"
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

mkdir -p "src/${PROJECT_NAME}/defs/catalog"

cat > "src/${PROJECT_NAME}/defs/catalog/defs.yaml" <<'YAML'
type: dagster_community_components.ComponentCatalogAgentComponent
attributes:
  plan_asset_name: catalog_plan
  execution_asset_name: catalog_execution
  synthesis_asset_name: catalog_answer
  task: |
    Generate two small synthetic datasets — one of customers (10 rows) and
    one of products (10 rows). Then describe in one paragraph what fields
    each dataset contains, using the actual field names.
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  # The manifest is the catalog. Filter narrow for a safe demo:
  include_ids: [synthetic_data_generator]
  max_picks: 3
  group_name: catalog_agent_demo
YAML

ok "Wrote defs.yaml"

DM="${PROJECT_NAME}.definitions"

info "Running catalog agent (planner + real component executions + synthesizer)…"
uv run dagster asset materialize --select 'catalog_plan+' -m "$DM" 2>&1 | tail -3 || fail "run failed"

echo
ok "Demo complete."
echo
cat <<EOF
The catalog agent just:
  1. Fetched the live manifest.json (~900 components)
  2. Filtered to just synthetic_data_generator (the include_ids field)
  3. Planner LLM saw the schema + description → picked 2 invocations with
     different config (customers + products) with the CORRECT field names
     (Pydantic model_fields introspection guides the planner)
  4. Executor imported SyntheticDataGeneratorComponent by class name from
     dagster_community_components, instantiated it with Pydantic, called
     build_defs(), and materialized the asset in-process via dg.materialize()
  5. Two REAL DataFrames landed as first-class Dagster materializations
  6. Synthesizer LLM wrote a description grounded in the REAL field names
     from the materialized data

Inspect:
  cd $PROJECT_NAME
  uv run dg dev
    → asset graph: catalog_plan → catalog_execution → catalog_answer
    → click catalog_plan for the planner's picks (component ids + configs)
    → click catalog_execution for the REAL materialization outputs (10 rows each)
    → click catalog_answer for the synthesized description

To broaden the catalog beyond just synthetic_data_generator, edit
defs.yaml. Try:
  include_categories: [ai]         # ~40 AI components
  include_tags: [transform]         # dozens of transform components
  # Or remove all filters (max_catalog_entries caps at 40 by default)

CAUTION: many components require external resources (Snowflake, Slack,
etc.) that won't be wired at runtime. Components that need resources will
raise on materialization — the demo logs the failure and continues. Set
fail_on_execution_error: true if you want the run to halt on first failure.
EOF
