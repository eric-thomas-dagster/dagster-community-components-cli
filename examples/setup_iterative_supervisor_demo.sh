#!/usr/bin/env bash
# setup_iterative_supervisor_demo.sh
#
# Iterative Supervisor Agent — chained tool use with per-step Dagster lineage.
#
# The chaining companion to Supervisor Agent. Single-shot Supervisor picks
# ALL tools upfront; the translator ended up with a placeholder for the
# math result because the planner couldn't see math's output yet. This
# demo fixes that: the planner runs EVERY step, sees prior tool outputs,
# and picks the NEXT tool call. Static DAG (max_iterations step assets
# pre-declared), dynamic termination (whichever step says `done` short-
# circuits later steps).
#
# Pipeline (one YAML block emits all of this):
#   agent_step_1              (planner sees task → picks 1st tool → runs it)
#         ↓
#   agent_step_2              (planner sees step 1 output → picks next OR done)
#         ↓
#   agent_step_3              (planner sees step 1+2 → picks next OR done)
#         ↓
#   agent_step_4              (probably done — short-circuits)
#         ↓
#   agent_step_5              (definitely done — short-circuits)
#         ↓
#   agent_final_answer        (synthesizer reads all trajectory, writes answer)
#
# The demo task requires chaining: compute 149 × 12 THEN translate the
# result to French. Single-shot Supervisor couldn't do this properly
# because the translator's input has to reference the math result. This
# demo shows the planner deciding math_expert first, seeing "1788", then
# picking translator with "1788 euros" as the concrete input.
#
# COST: ~$0.02 per run (planner + tool calls across ~2-3 non-noop steps)
#
# REQUIREMENTS
#   • uv, OPENAI_API_KEY
#
# USAGE
#   export OPENAI_API_KEY=sk-...
#   ./setup_iterative_supervisor_demo.sh          # → iterative_supervisor_demo/

set -eo pipefail

PROJECT_NAME="${1:-iterative_supervisor_demo}"
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

mkdir -p "src/${PROJECT_NAME}/defs/iterative_agent"

cat > "src/${PROJECT_NAME}/defs/iterative_agent/defs.yaml" <<'YAML'
type: dagster_community_components.IterativeSupervisorAgentComponent
attributes:
  step_asset_prefix: agent_step
  synthesis_asset_name: agent_final_answer
  task: |
    A customer emails us in French asking: "Combien coûte l'abonnement
    annuel à 149 euros par mois?" — plus they want the answer in French.
    Step through this: (1) compute the annual cost, (2) translate the
    result into a proper French sentence a customer would appreciate.
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  max_iterations: 5
  temperature: 0.1
  tools:
    - name: math_expert
      description: "Do arithmetic. Args: a math expression as a string."
      system_message: |
        You are a calculator. Given a math expression, return the number
        and a one-line explanation. Nothing else.

    - name: translator
      description: "Translate text between languages. Args: JSON object with keys 'text' and 'target_language'."
      system_message: |
        You are a translator. Given a JSON object with `text` and
        `target_language`, return ONLY the translated text — no
        explanation, no quotes, just the translation.

    - name: writer
      description: "Draft a polished, customer-friendly sentence given a fact. Args: the raw fact + intended tone."
      system_message: |
        You are a customer-communications writer. Given a raw fact and
        tone (e.g., "helpful, friendly, in French"), return one polished
        sentence a customer would appreciate.

  group_name: iterative_agent_demo
YAML

ok "Wrote defs.yaml"

DM="${PROJECT_NAME}.definitions"

info "Running iterative agent (up to 5 steps + synthesis)…"
uv run dagster asset materialize --select '*' -m "$DM" 2>&1 | tail -3 || fail "run failed"

echo
ok "Demo complete."
echo
cat <<EOF
The iterative supervisor pattern just ran end-to-end:
  1. agent_step_1: planner picked the first tool given only the task
  2. agent_step_2: planner SAW step 1's output and picked the next tool
  3. agent_step_3+: continued until planner said "done", then later
     steps short-circuited as no-ops
  4. agent_final_answer: synthesizer read the full trajectory and wrote
     the final answer

Inspect the audit trail (this is the ReAct loop as REAL DAGSTER ASSETS):
  cd $PROJECT_NAME
  uv run dg dev
    → asset graph: 5 step assets → synthesis
    → click each agent_step_N to see the planner's reasoning + tool call + output
    → skipped steps show 'short-circuit — prior step done' in metadata
    → click agent_final_answer for the polished French response

Compare to Supervisor Agent (single-shot):
  In that demo, the translator got a placeholder because the planner
  guessed the input BEFORE math_expert ran. In THIS demo, the translator
  gets the real "1788 euros" string because it runs after math_expert.
  That's the value of chaining.
EOF
