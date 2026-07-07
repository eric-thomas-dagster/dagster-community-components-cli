#!/usr/bin/env bash
# setup_supervisor_per_tool_model_demo.sh
#
# Supervisor Agent with per-tool model routing.
#
# Every earlier Supervisor demo used ONE model for all tools. That's fine
# for math + simple text, but tools like `web_search` HALLUCINATE — a plain
# gpt-4o-mini call can't actually browse the web, so it fabricates snippets.
#
# This demo fixes that: each tool can specify its OWN model + api_base. In
# particular, we route `web_search` to a search-capable model (Perplexity's
# sonar-pro via Vercel AI Gateway) while keeping cheap gpt-4o-mini for math.
#
# Pipeline:
#   supervisor_plan     (planner LLM picks tools + inputs)
#         ↓
#   ├── web_search_result   ← perplexity/sonar-pro (REAL web search)
#   └── math_expert_result  ← openai/gpt-4o-mini (arithmetic)
#         ↓
#   final_answer        (synthesizer LLM combines with source-cited answer)
#
# COST: ~$0.02-$0.05 (planner + tool calls; Perplexity is a bit pricier)
#
# REQUIREMENTS
#   • uv, VERCEL_AI_TOKEN (Vercel AI Gateway key, vck_...)
#     Create at your Vercel dashboard → AI Gateway → API Keys.
#     Account needs a positive credit balance.
#
# USAGE
#   export VERCEL_AI_TOKEN=vck_...
#   ./setup_supervisor_per_tool_model_demo.sh          # → supervisor_per_tool_model_demo/

set -eo pipefail

PROJECT_NAME="${1:-supervisor_per_tool_model_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

[ -z "${VERCEL_AI_TOKEN:-}" ] && fail "VERCEL_AI_TOKEN not set (need a Vercel AI Gateway key)."
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

# Export the base URL so the components can find it.
export VERCEL_AI_GATEWAY_URL="https://ai-gateway.vercel.sh/v1"

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
    Answer two questions in one response:
    1. Who currently leads Anthropic (the AI company)?
    2. What is 4217 multiplied by 89?
    Cite sources for factual claims.
  # Component-level defaults — used for planner + synthesizer + any tool
  # that doesn't override.
  model: openai/gpt-4o-mini
  api_key_env_var: VERCEL_AI_TOKEN
  api_base_env_var: VERCEL_AI_GATEWAY_URL
  temperature: 0.1
  max_picks: 3
  tools:
    - name: web_search
      description: "Search the current web for factual info with source citations."
      # Per-tool override: real search-capable model instead of gpt-4o-mini.
      model: perplexity/sonar-pro
      # Inherits api_key + api_base from component-level (Vercel AI Gateway).
      system_message: |
        You are a web search agent. Given a question, search the web and
        return a concise factual answer with source URLs. If you cannot
        verify the information, say so explicitly.

    - name: math_expert
      description: "Do arithmetic on a math expression."
      # No per-tool overrides — inherits component-level (openai/gpt-4o-mini via Vercel).
      system_message: |
        You are a calculator. Given a math expression, return the exact
        number and a one-line explanation of the calculation.
  group_name: per_tool_demo
YAML

ok "Wrote defs.yaml"

DM="${PROJECT_NAME}.definitions"
info "Running supervisor with per-tool model routing (web_search → perplexity/sonar-pro; math → gpt-4o-mini)…"
uv run dagster asset materialize --select '*' -m "$DM" 2>&1 | tail -3 || fail "run failed"

echo
ok "Demo complete."
echo
cat <<EOF
The supervisor just ran with MIXED MODEL ROUTING:
  1. Planner LLM (openai/gpt-4o-mini via Vercel) picked tools + inputs
  2. web_search tool ran against perplexity/sonar-pro (REAL web search)
  3. math_expert tool ran against openai/gpt-4o-mini (arithmetic)
  4. Synthesizer LLM (openai/gpt-4o-mini) combined with cited sources

Inspect:
  cd $PROJECT_NAME
  uv run dg dev
    → asset graph: supervisor_plan → web_search_result + math_expert_result → final_answer
    → click web_search_result to see REAL search snippets with URLs
    → click final_answer for the sourced synthesis

This is the pattern that gives you the flexibility of chained LLM tools
WITHOUT the hallucination problem — each tool uses the RIGHT model for
its job. Search-heavy tools use search-capable models; cheap tools use
cheap models.
EOF
