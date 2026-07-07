#!/usr/bin/env bash
# setup_adaptive_research_brief_demo.sh
#
# Adaptive Research Brief — planner LLM decides N sub-topics at RUNTIME.
#
# Where DynamicOutput / dynamic partitions fits. The Data Doctor / Triage
# / Supervisor demos all had FIXED action-space at pipeline-write time.
# This one is different: the LLM decides HOW MANY sub-topics to research
# based on what the topic actually needs. Might be 3, might be 12.
#
# Pipeline (one YAML block emits all 3 assets):
#   research_plan     (planner LLM: decides N and emits {angle, focus} per subtopic)
#         ↓
#   subtopic_notes    (row-wise LLM: writes a research note per subtopic)
#         ↓
#   research_brief    (synthesizer LLM: markdown brief with headings + exec summary)
#
# N is truly runtime-decided (bounded by max_subtopics YAML field).
# In `dg dev` you see the plan grow / shrink per run.
#
# COST: ~$0.01-$0.05 per run (planner + N × researcher + brief, all gpt-4o-mini)
#
# REQUIREMENTS
#   • uv, OPENAI_API_KEY
#
# USAGE
#   export OPENAI_API_KEY=sk-...
#   ./setup_adaptive_research_brief_demo.sh          # → adaptive_research_brief_demo/

set -eo pipefail

PROJECT_NAME="${1:-adaptive_research_brief_demo}"
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

mkdir -p "src/${PROJECT_NAME}/defs/brief"

cat > "src/${PROJECT_NAME}/defs/brief/defs.yaml" <<'YAML'
type: dagster_community_components.AdaptiveResearchBriefComponent
attributes:
  plan_asset_name: research_plan
  notes_asset_name: subtopic_notes
  brief_asset_name: research_brief
  topic: |
    Prepare a competitive brief on Anthropic (the AI company).
    Cover: product lineup, pricing model, safety approach, notable
    recent research directions, and how they differentiate from
    OpenAI / Google DeepMind. The audience is a Series-B startup
    CEO deciding which model provider to standardize on.
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  max_subtopics: 8
  temperature: 0.3
  group_name: research
YAML

ok "Wrote defs.yaml (AdaptiveResearchBriefComponent)"

DM="${PROJECT_NAME}.definitions"

info "Planner deciding N sub-topics (gpt-4o-mini)…"
uv run dagster asset materialize --select research_plan -m "$DM" 2>&1 | tail -3 || fail "plan failed"

info "Researching each subtopic (row-wise LLM)…"
uv run dagster asset materialize --select subtopic_notes -m "$DM" 2>&1 | tail -3 || fail "notes failed"

info "Synthesizing final markdown brief…"
uv run dagster asset materialize --select research_brief -m "$DM" 2>&1 | tail -3 || fail "brief failed"

echo
ok "Demo complete."
echo
cat <<EOF
The adaptive-N research pattern just ran:
  1. Planner LLM decided how many sub-topics to research (up to 8)
     and what angle/focus each should have — N is DECIDED AT RUNTIME
  2. Row-wise researcher LLM wrote a note per subtopic (N calls)
  3. Synthesizer LLM combined all notes into a markdown brief with
     headings + an executive summary

Inspect the audit trail:
  cd $PROJECT_NAME
  uv run dg dev
    → asset graph: research_plan → subtopic_notes → research_brief
    → click research_plan to see how many subtopics + their angles
    → click subtopic_notes to see each individual research note
    → click research_brief to see the final markdown

Rerun with a different topic (edit defs.yaml) — the planner will pick
a DIFFERENT N. That's the "adaptive" part.

Where DynamicOutput / dynamic partitions fits: pair the researcher
asset with a dynamic partitions definition and let the planner emit
partition keys per subtopic. Then EACH subtopic becomes its own
independently-retryable Dagster materialization (visible in the UI as
N materializations of the same asset). See the walkthrough.
EOF
