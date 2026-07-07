#!/usr/bin/env bash
# setup_supervisor_per_tool_model_demo.sh
#
# Supervisor Agent with per-tool model routing — real-world use case: pick
# the RIGHT model per tool for cost/capability.
#
# The problem this solves: previously the SupervisorAgent used ONE model
# across all tools. That means either everything is on cheap gpt-4o-mini
# (fine for math, but adversarial critic-style tools are weaker) OR
# everything runs on gpt-4o (more expensive than needed for simple tasks).
#
# The fix: each tool can override the component-level model. Use gpt-4o
# where you need stronger reasoning; use gpt-4o-mini where cheap is enough.
#
# Pipeline (same as previous supervisor demos, just with per-tool models):
#   supervisor_plan     (planner LLM picks tools + inputs)
#         ↓
#   ├── critic_result           ← gpt-4o             (stronger reasoning)
#   ├── math_expert_result      ← gpt-4o-mini        (cheap arithmetic)
#   └── translator_result       ← gpt-4o-mini        (cheap translation)
#         ↓
#   final_answer         (synthesizer LLM combines)
#
# COST: ~$0.03 per run — driven mostly by the gpt-4o critic call.
#
# REQUIREMENTS
#   • uv, OPENAI_API_KEY (that's it — no third-party gateway)
#
# USAGE
#   export OPENAI_API_KEY=sk-...
#   ./setup_supervisor_per_tool_model_demo.sh          # → supervisor_per_tool_model_demo/

set -eo pipefail

PROJECT_NAME="${1:-supervisor_per_tool_model_demo}"
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

cat > "src/${PROJECT_NAME}/defs/supervisor/defs.yaml" <<'YAML'
type: dagster_community_components.SupervisorAgentComponent
attributes:
  plan_asset_name: supervisor_plan
  synthesis_asset_name: final_answer
  task: |
    A customer wrote in French: "Est-ce que 4217 * 89 = 375013 est correct?"
    (Is 4217 * 89 = 375013 correct?)
    Please: (1) do the arithmetic to verify, (2) translate the customer's
    question and the correct answer into English, and (3) have a critic
    review the drafted response for any subtle errors before we send it.
  # Component-level defaults — used for planner + synthesizer + any tool
  # that doesn't override. Just plain OpenAI, no gateway.
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  temperature: 0.1
  max_picks: 4
  tools:
    - name: math_expert
      description: "Do arithmetic on a math expression."
      # No override — inherits gpt-4o-mini. Math is cheap.
      system_message: |
        You are a calculator. Given a math expression, return the exact
        number and a one-line explanation.

    - name: translator
      description: "Translate text between languages."
      # No override — inherits gpt-4o-mini. Translation is fine on cheap.
      system_message: |
        You are a translator. Given a JSON object with `text` and `target_language`,
        return ONLY the translated text.

    - name: critic
      description: "Adversarial review — catch subtle errors, unit confusion, wrong assumptions."
      # PER-TOOL OVERRIDE — critique benefits from stronger reasoning.
      model: gpt-4o
      # api_key_env_var / api_base_env_var inherited from component-level.
      system_message: |
        You are an adversarial critic. Given a drafted response, point out
        any potential issues: arithmetic errors, translation nuances,
        implicit assumptions, tone problems. Be terse but specific.
  group_name: per_tool_demo
YAML

ok "Wrote defs.yaml"

DM="${PROJECT_NAME}.definitions"
info "Running supervisor with per-tool model routing (critic → gpt-4o; others → gpt-4o-mini)…"
uv run dagster asset materialize --select '*' -m "$DM" 2>&1 | tail -3 || fail "run failed"

echo
ok "Demo complete."
echo
cat <<EOF
The supervisor ran with per-tool model routing:
  planner       → gpt-4o-mini (cheap, picks tools)
  math_expert   → gpt-4o-mini (cheap, arithmetic is fine)
  translator    → gpt-4o-mini (cheap, works on translation)
  critic        → gpt-4o      (STRONGER — catches subtle errors)
  synthesizer   → gpt-4o-mini (cheap, just weaves the parts)

Inspect:
  cd $PROJECT_NAME
  uv run dg dev
    → click each *_result asset — asset metadata shows which model ran
    → critic_result should be a more careful review than gpt-4o-mini would give

If you want to swap any tool to a REAL search-capable model (Perplexity,
OpenAI Responses API, xAI Grok, etc.), just override that tool's fields:

  tools:
    - name: web_search
      description: "Search the web for citations."
      model: sonar-pro                          # or another search-native model
      api_key_env_var: PERPLEXITY_API_KEY       # provider-specific key
      api_base_env_var: PERPLEXITY_BASE_URL     # base URL (OpenAI-compatible)
      system_message: "..."

The mechanic is generic — any OpenAI-compatible endpoint works.
EOF
