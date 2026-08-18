#!/usr/bin/env bash
# pca_investment_memo — the "3-analyst debate" pattern, PCA-authored.
#
# PlannedCatalogAgentComponent takes an NL task, LLM planner picks
# agentic_pipeline from the community registry, emits a debate pipeline
# with 3 proposers + arbitrator. Cached to state; every subsequent run
# is pure Dagster with no LLM planner cost.
#
# ## Needs
#   - OPENAI_API_KEY
#   - uv
#
# ## Cost
#   ~$0.0006 for the planner trajectory, ~$0.001 per materialize.

set -eo pipefail

PROJECT_DIR="${1:-pca-investment-memo-demo}"
COMMIT_SHA="${COMMIT_SHA:-main}"

if ! command -v uv >/dev/null 2>&1; then echo "✗ uv required"; exit 1; fi
if [ -z "$OPENAI_API_KEY" ]; then
  echo "! OPENAI_API_KEY not set — planner + pipeline will fail at run time."
fi

info()  { echo "→ $*"; }
ok()    { echo "✓ $*"; }
fail()  { echo "✗ $*"; exit 1; }

rm -rf "$PROJECT_DIR"
info "scaffolding Dagster project…"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync 2>&1 | tail -2
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"
PKG="$(basename "$PROJECT_ABS" | tr '-' '_')"

if [ -n "$DCC_LOCAL_PATH" ]; then
  DCC_SRC="dagster-community-components @ file://$DCC_LOCAL_PATH"
else
  DCC_SRC="dagster-community-components @ https://github.com/eric-thomas-dagster/dagster-component-templates/archive/$COMMIT_SHA.zip"
fi
export DAGSTER_HOME="$PROJECT_ABS/.dagster_home"
mkdir -p "$DAGSTER_HOME"

info "installing deps…"
uv add -q "$DCC_SRC" openai 'litellm>=1.30.0' 'mcp>=1.0.0' pandas 2>&1 | tail -1

# --- THE PROMPT ---
mkdir -p "src/$PKG/defs/investment_memo"
cat > "src/$PKG/defs/investment_memo/defs.yaml" <<'EOF'
type: dagster_community_components.PlannedCatalogAgentComponent
attributes:
  task: |
    Emit ONE agentic_pipeline component with this shape:

    - asset_name_prefix: investment_memo
    - source: {kind: literal, text: "Investment committee memo for ticker NVDA. Buy, hold, or sell?"}
    - steps:
        - id: recommendation
          op: debate
          proposers:
            - {model: gpt-4o-mini, api_key_env_var: OPENAI_API_KEY,
               system_prompt: "You are a bull analyst. Argue for BUY. One paragraph.",
               temperature: 0.8, max_tokens: 300}
            - {model: gpt-4o-mini, api_key_env_var: OPENAI_API_KEY,
               system_prompt: "You are a bear analyst. Argue for SELL. One paragraph.",
               temperature: 0.8, max_tokens: 300}
            - {model: gpt-4o-mini, api_key_env_var: OPENAI_API_KEY,
               system_prompt: "You are a neutral analyst. Argue for HOLD with a target price range. One paragraph.",
               temperature: 0.8, max_tokens: 300}
          arbitrator:
            model: gpt-4o-mini
            api_key_env_var: OPENAI_API_KEY
            system_prompt: "Pick the recommendation best for a moderate-risk, long-horizon portfolio."
    - outputs:
        assets: [recommendation]

  include_ids: [agentic_pipeline]
  task_hints:
    - "Emit exactly ONE agentic_pipeline component."
    - "Use the `debate` op — 3 proposers (bull / bear / neutral) + 1 arbitrator."
EOF
ok "wrote the task (investment_memo/defs.yaml)"

info "PCA planner running (one-time)…"
uv run dg utils refresh-defs-state 2>&1 | grep -E "planned_agent|refreshed|✓" | tail -5

DM="${PKG}.definitions"
info "assets emitted:"
uv run dagster asset list -m "$DM" 2>&1 | grep -v -E "WARNING|VIRTUAL_ENV|^$" | head -5

echo
ok "Setup complete."
echo
cat <<EOF
Now:
  cd $PROJECT_ABS
  DAGSTER_HOME=$DAGSTER_HOME uv run dg dev

Or headless:
  DAGSTER_HOME=$DAGSTER_HOME uv run dagster asset materialize \\
    --select investment_memo_recommendation -m $DM

The prompt lives at:
  $PROJECT_ABS/src/$PKG/defs/investment_memo/defs.yaml
EOF
