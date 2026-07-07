#!/usr/bin/env bash
# setup_supervisor_agent_demo.sh
#
# Supervisor Agent — the AGENT picks which agents to call.
#
# The "agent of agents" shape. A planner LLM reads a task and picks WHICH
# specialist tools to invoke from a bounded YAML-declared set. Each tool
# is a separate Dagster asset with its own LLM persona. A synthesizer
# reads all tool outputs and writes the final answer.
#
# What makes this SAFE:
#   • The tool set is FIXED in YAML at pipeline-write time.
#   • The planner picks BY NAME. It cannot invent tools, write code, or
#     escape the sandbox.
#   • Every pick + reason is a Dagster asset — full lineage in `dg dev`.
#
# Pipeline (one YAML block emits ALL of this):
#   supervisor_plan       (planner LLM picks tools + tool_inputs)
#         ↓
#   ├── web_search_result       (LLM persona: web search)
#   ├── kb_expert_result        (LLM persona: docs QA)
#   ├── math_expert_result      (LLM persona: calculator)
#   ├── translator_result       (LLM persona: translator)
#   └── critic_result           (LLM persona: adversarial critic)
#         ↓
#   final_answer          (synthesizer LLM reads all tool outputs + task)
#
# The tools the planner DIDN'T pick still materialize — as empty
# DataFrames — so the DAG shape stays static across runs. This trades
# TRUE dynamic fan-out (via DynamicOutput / dynamic partitions) for a
# demoable, visually-consistent asset graph. See the walkthrough for
# the dynamic-partitions variant.
#
# COST: ~$0.005-$0.02 per run (1 planner call + up to 4 tool calls +
# 1 synthesis call, all on gpt-4o-mini)
#
# REQUIREMENTS
#   • uv, OPENAI_API_KEY
#
# USAGE
#   export OPENAI_API_KEY=sk-...
#   ./setup_supervisor_agent_demo.sh              # → supervisor_agent_demo/

set -eo pipefail

PROJECT_NAME="${1:-supervisor_agent_demo}"
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

mkdir -p "src/${PROJECT_NAME}/defs/supervisor"

# One YAML block emits: plan + 5 tool assets + synthesis
cat > "src/${PROJECT_NAME}/defs/supervisor/defs.yaml" <<'YAML'
type: dagster_community_components.SupervisorAgentComponent
attributes:
  plan_asset_name: supervisor_plan
  synthesis_asset_name: final_answer
  task: |
    A customer emails us in French asking: "Combien coûte l'abonnement annuel
    de 149 euros par mois?" (How much does the annual subscription cost at
    149 euros per month?) — plus they want to compare it to competitor pricing.
    Give a customer-friendly answer that addresses the pricing math and the
    competitive context.
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  temperature: 0.2
  tools:
    - name: web_search
      description: "Search the current web for recent pricing / competitor info."
      system_message: |
        You are a web search agent. Given a query, return 2-3 relevant
        factual snippets with plausible source names (Company X pricing page,
        review site, etc.) as if you actually searched. Keep it concise.

    - name: math_expert
      description: "Do arithmetic on numbers cited in the task (currency, totals, discounts, etc.)."
      system_message: |
        You are a calculation agent. Given a math request, return the
        answer AND a one-line explanation of the calculation. Be precise.

    - name: translator
      description: "Translate between languages so the answer meets the customer's language."
      system_message: |
        You are a translator. Given text, translate to the target language
        specified in the request. If no target given, default to English.

    - name: kb_expert
      description: "Answer from internal Dagster+/pricing docs (governed KB source)."
      system_message: |
        You are a docs-QA agent. Answer strictly from the following KB
        summary: Dagster+ offers 3 tiers (Solo $29/mo, Standard $99/mo,
        Pro $499/mo). Annual is billed monthly. If unsure, say so.

    - name: critic
      description: "Adversarial critic — sanity-check the plan before synthesis."
      system_message: |
        You are an adversarial critic. Given a question, point out 1-2
        ways an answer could go wrong (unit confusion, misread question,
        missing context) so the synthesizer can preempt them.

  group_name: supervisor_demo
YAML

ok "Wrote defs.yaml (SupervisorAgentComponent — 5 tools)"

DM="${PROJECT_NAME}.definitions"

info "Planner picking tools (gpt-4o-mini)…"
uv run dagster asset materialize --select supervisor_plan -m "$DM" 2>&1 | tail -3 || fail "plan failed"

info "Executing per-tool assets in parallel…"
uv run dagster asset materialize --select 'supervisor_plan+' -m "$DM" 2>&1 | tail -3 || fail "tools failed"

info "Synthesizing final answer…"
uv run dagster asset materialize --select final_answer -m "$DM" 2>&1 | tail -3 || fail "synthesis failed"

echo
ok "Demo complete."
echo
cat <<EOF
The supervisor pattern just ran:
  1. Planner LLM read the task, picked a subset of the 5 available tools
     with a REASON per pick
  2. Only the picked tools invoked (others materialized as empty)
  3. Synthesizer LLM read all tool outputs + task and wrote the final answer

Inspect the audit trail:
  cd $PROJECT_NAME
  uv run dg dev
    → asset graph: supervisor_plan → 5 tool assets → final_answer
    → click supervisor_plan → the planner's picks + reasons
    → click each *_result asset → see the tool's LLM output
    → click final_answer → the grounded synthesized response

The key primitive: SupervisorAgentComponent gives you the "agent of
agents" shape with FULL Dagster lineage. The tool set is bounded at
YAML load time. The planner picks BY NAME — no arbitrary code, no
tool invention. Every choice is auditable.
EOF
