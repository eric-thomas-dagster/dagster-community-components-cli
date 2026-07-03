#!/usr/bin/env bash
# setup_langgraph_agent_demo.sh
#
# Scaffolds a fresh Dagster project containing a 4-step LangGraph research
# pipeline (plan → research → critique → synthesize) as a single asset.
#
# Every step is a real OpenAI call — no mocks. Runs entirely on a free-tier
# API key. Typical end-to-end run: ~15 seconds, ~1500-2500 tokens (< $0.01
# on gpt-4o-mini).
#
# What it demonstrates
#   • LangGraphAgentComponent as a stateful multi-node graph (not a chain)
#   • Cross-step state references via {outputs.<step_name>}
#   • Asset metadata surfacing the full per-step transcript
#
# Requirements
#   • dg CLI (>= 1.11)
#   • Python 3.10+
#   • $OPENAI_API_KEY set in your shell
#   • Internet access to api.openai.com
#
# Cost / privacy
#   Runs against your OpenAI account. No customer data leaves your box; the
#   demo asks a public engineering question.
#
# Usage
#   ./setup_langgraph_agent_demo.sh                       # project → langgraph_demo
#   ./setup_langgraph_agent_demo.sh my_research_agent     # custom project name

set -eo pipefail

PROJECT_NAME="${1:-langgraph_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_YELLOW='\033[0;33m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
warn()  { echo -e "${C_YELLOW}⚠${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

# ── Preflight ────────────────────────────────────────────────────────────────
[ -z "${OPENAI_API_KEY:-}" ] && fail "OPENAI_API_KEY not set. Get one at https://platform.openai.com/api-keys and export it."
command -v uvx >/dev/null 2>&1 || fail "uvx not found. Install uv: https://docs.astral.sh/uv/"
command -v python3 >/dev/null 2>&1 || fail "python3 not found."

if [ -d "$PROJECT_DIR" ]; then
  fail "Directory already exists: $PROJECT_DIR  (pick a different name)"
fi

info "OPENAI_API_KEY: set (prefix ${OPENAI_API_KEY:0:8}…)"
info "Target project: $PROJECT_DIR"

# ── Scaffold ─────────────────────────────────────────────────────────────────
info "Scaffolding Dagster project…"
uvx create-dagster project "$PROJECT_DIR" --uv-sync 2>&1 | tail -5 || fail "create-dagster failed"
cd "$PROJECT_DIR"

info "Installing dependencies (dagster-community-components + langgraph + langchain-openai)…"
# DCC_SRC lets a dev point at a local source checkout of
# dagster-community-components (useful when iterating locally). Otherwise
# we install from git main — PyPI lags behind the registry, so pinning
# to main is the way to get the freshest components today.
if [ -n "${DCC_SRC:-}" ] && [ -d "$DCC_SRC" ]; then
  info "Using local DCC source: $DCC_SRC"
  uv add --quiet \
    "dagster-community-components @ ${DCC_SRC}" \
    'langgraph>=0.2.0' 'langchain-core>=0.3.0' 'langchain-openai>=0.2.0' \
    || fail "uv add failed"
else
  uv add --quiet \
    'dagster-community-components @ git+https://github.com/eric-thomas-dagster/dagster-component-templates.git' \
    'langgraph>=0.2.0' 'langchain-core>=0.3.0' 'langchain-openai>=0.2.0' \
    || fail "uv add failed"
fi
ok "Dependencies installed"

# ── Write the LangGraph defs ────────────────────────────────────────────────
DEFS_DIR="src/${PROJECT_NAME}/defs/research_agent"
mkdir -p "$DEFS_DIR"

cat > "$DEFS_DIR/defs.yaml" <<'YAML'
type: dagster_community_components.LangGraphAgentComponent
attributes:
  asset_name: research_report
  input_prompt: "How do modern vector databases handle high-cardinality metadata filters efficiently without collapsing recall?"
  llm_provider: openai
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  system_message: "You are a rigorous senior engineer. Be specific and cite techniques by name."
  temperature: 0.2
  max_tokens: 700
  group_name: research

  steps:
    - name: plan
      prompt: |
        Break the question below into EXACTLY 3 focused sub-questions that
        collectively cover the space. Output ONLY a numbered list (no preamble,
        no trailing commentary).

        Question: {input}
      next: research

    - name: research
      prompt: |
        Answer each sub-question below in 2-3 sentences. Name specific
        algorithms, data structures, or papers where relevant. Do NOT
        hallucinate citations — if you don't know a source, say so.

        Sub-questions:
        {outputs.plan}
      next: critique

    - name: critique
      prompt: |
        Grade the research below on rigor (0-10). List any missing angles
        in 1-2 bullets. Keep it to 3 lines total.

        Original question: {input}
        Research:
        {outputs.research}
      next: synthesize

    - name: synthesize
      prompt: |
        Combine the findings and the critique into a single tight paragraph
        (max 5 sentences) that DIRECTLY answers the original question.

        Original question: {input}
        Findings:
        {outputs.research}
        Critique:
        {outputs.critique}
YAML

ok "Wrote $DEFS_DIR/defs.yaml"

# ── Materialize ─────────────────────────────────────────────────────────────
info "Materializing research_report asset (4 LLM calls, ~15s)…"
DAGSTER_MODULE="${PROJECT_NAME}.definitions"
uv run dagster asset materialize --select research_report -m "$DAGSTER_MODULE" 2>&1 | tail -40 || fail "materialize failed"
ok "Asset materialized"

# ── Report ──────────────────────────────────────────────────────────────────
echo
ok "Demo complete."
echo
cat <<EOF
Next steps:
  cd $PROJECT_NAME
  dg dev                       # open the Dagster UI to see the full transcript
  # then browse to the research_report asset and expand its metadata:
  #   • final_answer (markdown)
  #   • steps_run  (plan → research → critique → synthesize)
  #   • step_outputs (JSON blob of every intermediate LLM response)

Tune the pipeline:
  edit ${DEFS_DIR}/defs.yaml
    - change input_prompt for a different research question
    - add a step with condition_regex to branch on classification output
    - swap model to gpt-4o for higher quality (10x cost)
EOF
