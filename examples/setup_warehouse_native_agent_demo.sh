#!/usr/bin/env bash
# setup_warehouse_native_agent_demo.sh
#
# A warehouse-native NL->SQL agent (Snowflake Cortex COMPLETE, in this case)
# is powerful but you don't want it silently acting on a wrong query or a
# bad answer. This demo chains:
#
#   warehouse_answer (snowflake_cortex_agent) -> answer_sanity_check (llm_judge)
#       -> answer_approved (human_approval_gate)
#
# An automated judge scores the agent's answer BEFORE a human has to look at
# it, and nothing downstream can consume the answer until a human (or a
# webhook/Slack bot/ServiceNow ticket -- anything that can write a JSON file)
# explicitly approves it.
#
# HONESTY NOTE: snowflake_cortex_agent calls the real Snowflake Cortex REST
# API -- there is no demo_mode (confirmed by reading its component.py; this
# component genuinely has no free/local path, unlike most of this registry).
# This script WILL fail at the warehouse_answer step without a real Snowflake
# account with Cortex enabled. `dg check defs` passes (config is structurally
# valid) without one. The downstream judge + approval-gate mechanics were
# live-verified in a separate harness using a stand-in answer (the exact
# shape snowflake_cortex_agent's own metadata/output takes), confirmed via a
# real two-phase `dg launch`: once with no approval token (check fails,
# status=approval_pending) and once after dropping a token (check passes).
#
# What it demonstrates
#   • snowflake_cortex_agent — single-shot NL->SQL agent via Cortex COMPLETE
#   • llm_judge — automated sanity-check BEFORE a human looks at it
#   • human_approval_gate — nothing downstream consumes the answer until a
#     JSON approval token is dropped (anything can write it: a human, a
#     Slack bot, a ServiceNow webhook, a GitHub Action)
#
# REQUIRED ENV
#   SNOWFLAKE_ACCOUNT_URL, SNOWFLAKE_PAT   real Snowflake account + Cortex access
#   OPENAI_API_KEY                          for the llm_judge step
#
# COST  Snowflake Cortex usage (varies by account) + ~$0.001 for one judge call.
#
# Requirements
#   • uv (https://docs.astral.sh/uv/)
#   • A Snowflake account with Cortex enabled and a personal access token
#
# Usage
#   ./setup_warehouse_native_agent_demo.sh                  # -> warehouse_native_agent_demo/
#   ./setup_warehouse_native_agent_demo.sh my_project         # custom name

set -eo pipefail

PROJECT_NAME="${1:-warehouse_native_agent_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_RED='\033[0;31m'; C_YELLOW='\033[1;33m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
warn()  { echo -e "${C_YELLOW}!${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

command -v uvx >/dev/null 2>&1 || fail "uvx not found."
[ -z "${OPENAI_API_KEY:-}" ] && fail "set OPENAI_API_KEY (used for the llm_judge step)."
if [ -z "${SNOWFLAKE_ACCOUNT_URL:-}" ] || [ -z "${SNOWFLAKE_PAT:-}" ]; then
  warn "SNOWFLAKE_ACCOUNT_URL / SNOWFLAKE_PAT not set -- the project will scaffold and"
  warn "'dg check defs' will pass, but 'dg launch' will fail at warehouse_answer without"
  warn "a real Snowflake account with Cortex enabled. snowflake_cortex_agent has no"
  warn "demo/offline mode (confirmed by reading its source) -- this is a genuine"
  warn "real-account requirement, not a bug in this demo."
fi
[ -d "$PROJECT_DIR" ] && fail "Directory exists: $PROJECT_DIR"

info "Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

info "Installing deps (pandas)…"
uv add -q pandas
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

info "Installing components (--refresh on first call busts the registry-manifest cache)"
$CLI --refresh add snowflake_cortex_agent --auto-install
$CLI add llm_judge                        --auto-install
$CLI add human_approval_gate              --auto-install

info "Writing defs.yaml (warehouse agent -> judge -> human approval gate)"

cat > "src/$PKG/defs/snowflake_cortex_agent/defs.yaml" <<EOF
type: $PKG.components.snowflake_cortex_agent.component.SnowflakeCortexAgentComponent
attributes:
  asset_name: warehouse_answer
  prompt: "What were total sales last quarter by region? Query the SALES_FACT table."
  model: claude-3-5-sonnet
  account_url_env_var: SNOWFLAKE_ACCOUNT_URL
  auth_token_env_var: SNOWFLAKE_PAT
  max_iterations: 6
  group_name: warehouse_agent
EOF

info "Writing a small adapter asset (warehouse_answer's dict output -> a 1-row DataFrame llm_judge can score)"
mkdir -p "src/$PKG/defs/warehouse_answer_as_dataframe"
cat > "src/$PKG/defs/warehouse_answer_as_dataframe/__init__.py" <<'PYEOF'
import pandas as pd
import dagster as dg


@dg.asset(deps=["warehouse_answer"], group_name="warehouse_agent")
def warehouse_answer_df(context: dg.AssetExecutionContext) -> pd.DataFrame:
    """snowflake_cortex_agent materializes a dict ({final_answer, iterations,
    tool_calls_made, ...}), not a DataFrame -- llm_judge needs a DataFrame with
    a response_column. This reads the real materialized value back out."""
    mat = context.instance.get_latest_materialization_event(dg.AssetKey("warehouse_answer"))
    answer = "(no materialization found)"
    if mat and mat.asset_materialization:
        md = mat.asset_materialization.metadata or {}
        if "final_answer" in md:
            answer = md["final_answer"].value
    return pd.DataFrame({"answer": [answer]})


defs = dg.Definitions(assets=[warehouse_answer_df])
PYEOF

cat > "src/$PKG/defs/llm_judge/defs.yaml" <<EOF
type: $PKG.components.llm_judge.component.LlmJudgeComponent
attributes:
  asset_name: answer_sanity_check
  upstream_asset_key: warehouse_answer_df
  response_column: answer
  criteria: [accuracy, clarity, completeness]
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  group_name: warehouse_agent
EOF

cat > "src/$PKG/defs/human_approval_gate/defs.yaml" <<EOF
type: $PKG.components.human_approval_gate.component.HumanApprovalGateComponent
attributes:
  asset_name: answer_approved
  upstream_asset_key: answer_sanity_check
  approval_dir: ./approvals
  default_approval_key: default
  group_name: human_in_the_loop
EOF

ok "Wrote 4 defs.yaml + 1 adapter asset"

info "Verifying the project loads"
uv run dg check defs 2>&1 | tail -5

if [ -z "${SNOWFLAKE_ACCOUNT_URL:-}" ] || [ -z "${SNOWFLAKE_PAT:-}" ]; then
  warn "Skipping 'dg launch' -- no real Snowflake credentials set (see note above)."
  echo
  ok "Setup complete (structural validation only)."
  echo "Set SNOWFLAKE_ACCOUNT_URL + SNOWFLAKE_PAT and run:"
  echo "  cd $PROJECT_NAME && uv run dg launch --assets '*'"
  exit 0
fi

info "Materializing warehouse_answer -> answer_sanity_check -> answer_approved (pending)"
uv run dg launch --assets '*' 2>&1 | tail -20

echo
ok "First pass complete — answer_approved materialized with status=approval_pending."
echo
cat <<EOF
Approve it:
  echo '{"approved": true, "approver": "you@example.com", "reason": "SQL and totals look right"}' \\
    > $PROJECT_NAME/approvals/default.json
  cd $PROJECT_NAME && uv run dg launch --assets answer_approved

Inspect the results:
  uv run dg dev
    → click warehouse_answer → see the agent's final_answer + tool calls
    → click answer_sanity_check → judge_score / judge_reason
    → click answer_approved → Checks tab: 'approved' flips from pending to
      passed once the token is dropped

Production variants:
  • Swap human_approval_gate for multi_approver_gate for N-of-M sign-off
  • Pair approval_dir with FilesystemMonitorSensorComponent to
    auto-progress the graph the instant a token drops (no manual re-launch)
  • Swap snowflake_cortex_agent for databricks_genie_query (needs a
    pre-created Genie space in the Databricks UI first)
EOF
