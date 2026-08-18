#!/usr/bin/env bash
# pca_research_bot — the "AI-authored pipeline as UI experience" demo.
#
# PlannedCatalogAgentComponent takes a natural-language task field, invokes
# an LLM planner at PREPARE time, picks a component from the ~960-component
# community registry, and caches the plan to Dagster's state store.
#
# From then on, every load reads the cached plan and emits REAL Dagster
# assets — zero LLM cost per run. Edit the `task:` string + run
# `dg utils refresh-defs-state` to re-plan.
#
# ## What the customer types (the whole demo)
#
# The `task:` field below IS the artifact. The rest is standard
# create-dagster scaffolding.
#
# ## Needs
#   - OPENAI_API_KEY (planner + emitted pipeline both use OpenAI)
#   - uv
#
# ## Cost
#   ~$0.0007 for the planner trajectory (one-time), ~$0.005 per materialize.

set -eo pipefail

PROJECT_DIR="${1:-pca-research-bot-demo}"
COMMIT_SHA="${COMMIT_SHA:-main}"

if ! command -v uv >/dev/null 2>&1; then echo "✗ uv required"; exit 1; fi
if [ -z "$OPENAI_API_KEY" ]; then
  echo "! OPENAI_API_KEY not set — planner + pipeline will fail at run time."
fi

info()  { echo "→ $*"; }
ok()    { echo "✓ $*"; }
fail()  { echo "✗ $*"; exit 1; }

# --- 1. fresh project ------------------------------------------------------
rm -rf "$PROJECT_DIR"
info "scaffolding Dagster project…"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync 2>&1 | tail -2
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"
PKG="$(basename "$PROJECT_ABS" | tr '-' '_')"

# --- 2. deps + env ---------------------------------------------------------
if [ -n "$DCC_LOCAL_PATH" ]; then
  DCC_SRC="dagster-community-components @ file://$DCC_LOCAL_PATH"
else
  DCC_SRC="dagster-community-components @ https://github.com/eric-thomas-dagster/dagster-component-templates/archive/$COMMIT_SHA.zip"
fi
export DAGSTER_HOME="$PROJECT_ABS/.dagster_home"
mkdir -p "$DAGSTER_HOME"

info "installing deps…"
uv add -q "$DCC_SRC" openai 'litellm>=1.30.0' 'mcp>=1.0.0' pandas 2>&1 | tail -1

# --- 3. THE PROMPT ---------------------------------------------------------
# This is the whole demo. Everything below is just plumbing.
mkdir -p "src/$PKG/defs/research_bot"
cat > "src/$PKG/defs/research_bot/defs.yaml" <<'EOF'
type: dagster_community_components.PlannedCatalogAgentComponent
attributes:
  task: |
    Emit ONE agentic_pipeline component with this shape (5 steps, 1 asset per step):

    - asset_name_prefix: research_bot
    - source: {kind: literal, text: "Explain how transformer attention works"}
    - steps:
        - id: baseline
          op: llm_call
          source: source
          model: gpt-4o-mini
          api_key_env_var: OPENAI_API_KEY
          system_prompt: "Explain the topic technically. One paragraph."
          max_tokens: 300
        - id: critique
          op: llm_call
          source: baseline
          model: gpt-4o-mini
          api_key_env_var: OPENAI_API_KEY
          system_prompt: "Critique the input for clarity and correctness. One paragraph."
          max_tokens: 200
        - id: rewrite_accessible
          op: llm_call
          source: baseline
          model: gpt-4o-mini
          api_key_env_var: OPENAI_API_KEY
          system_prompt: "Rewrite the input for a beginner audience. One paragraph."
          max_tokens: 300
        - id: rewrite_precise
          op: llm_call
          source: baseline
          model: gpt-4o-mini
          api_key_env_var: OPENAI_API_KEY
          system_prompt: "Rewrite the input to be more rigorous and formula-friendly. One paragraph."
          max_tokens: 300
        - id: final
          op: synthesize
          sources: [baseline, critique, rewrite_accessible, rewrite_precise]
          model: gpt-4o
          api_key_env_var: OPENAI_API_KEY
          system_prompt: "Merge the labeled sections into a single polished answer."
    - outputs:
        assets: [baseline, critique, rewrite_accessible, rewrite_precise, final]
        text_sinks:
          - {from: final, path: /tmp/pca_research_final.txt}

  include_ids: [agentic_pipeline]
  task_hints:
    - "Emit exactly ONE agentic_pipeline component instance."
    - "The 5 steps chain by `source: <step_id>`. `synthesize` uses `sources: [<step_ids>]` for its fan-in."
EOF
ok "wrote the task (research_bot/defs.yaml)"

# --- 4. run the planner ---------------------------------------------------
info "PCA planner running (one-time)…"
uv run dg utils refresh-defs-state 2>&1 | grep -E "planned_agent|refreshed|✓" | tail -5

# --- 5. show what PCA built ------------------------------------------------
DM="${PKG}.definitions"
info "assets emitted:"
uv run dagster asset list -m "$DM" 2>&1 | grep -v -E "WARNING|VIRTUAL_ENV|^$" | head -10

echo
ok "Setup complete."
echo
cat <<EOF
Now:
  cd $PROJECT_ABS
  DAGSTER_HOME=$DAGSTER_HOME uv run dg dev

Or headless:
  DAGSTER_HOME=$DAGSTER_HOME uv run dagster asset materialize \\
    --select 'research_bot_baseline,research_bot_critique,research_bot_rewrite_accessible,research_bot_rewrite_precise,research_bot_final' \\
    -m $DM

The prompt lives at:
  $PROJECT_ABS/src/$PKG/defs/research_bot/defs.yaml

Change it + run \`dg utils refresh-defs-state\` to re-plan.
EOF
