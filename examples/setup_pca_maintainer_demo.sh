#!/usr/bin/env bash
# pca_maintainer — the "AI maintainer investigation room," PCA-authored.
#
# PlannedCatalogAgentComponent takes an NL task, the planner picks
# agentic_pipeline v2 (with the new mcp_call op + typed named inputs),
# and emits a real GitHub-MCP-driven triage pipeline.
#
# Made possible by TWO PCA enhancements shipped this week:
#   1. `loop_guard_max_failures: 6` — meta-components need more iterations
#   2. `agent_hints.steps_schemas` on agentic_pipeline — strict per-op JSON
#      Schema published to the manifest; PCA injects it into the planner's
#      system prompt so field names (op vs type, mcp_tool_name vs tool,
#      tool_args vs params) can't drift.
#
# ## What the customer types
#
# The `task:` field below IS the artifact — the whole rest is plumbing.
#
# ## Needs
#   - OPENAI_API_KEY
#   - GITHUB_PERSONAL_ACCESS_TOKEN (any classic PAT with public_repo scope
#     — the stdio MCP subprocess inherits it from the shell)
#   - npx (Node — for the GitHub MCP server)
#   - uv
#
# ## Cost
#   ~$0.07 for the planner trajectory (one-time), ~$0.01 per materialize.
#   PCA needs 6-8 iterations to converge on a valid agentic_pipeline config;
#   this cost drops as we harden the schema hints further.

set -eo pipefail

PROJECT_DIR="${1:-pca-maintainer-demo}"
COMMIT_SHA="${COMMIT_SHA:-main}"
DAGSTER_ISSUE_NUM="${DAGSTER_ISSUE_NUM:-30000}"

if ! command -v uv >/dev/null 2>&1; then echo "✗ uv required"; exit 1; fi
if ! command -v npx >/dev/null 2>&1; then echo "✗ npx required (Node.js)"; exit 1; fi
if [ -z "$OPENAI_API_KEY" ]; then
  echo "! OPENAI_API_KEY not set — planner + pipeline will fail at run time."
fi
if [ -z "$GITHUB_PERSONAL_ACCESS_TOKEN" ]; then
  echo "! GITHUB_PERSONAL_ACCESS_TOKEN not set — GitHub MCP call will fail."
  echo "  Create one: https://github.com/settings/tokens/new"
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
mkdir -p "src/$PKG/defs/mir"
cat > "src/$PKG/defs/mir/defs.yaml" <<EOF
type: dagster_community_components.PlannedCatalogAgentComponent
attributes:
  task: |
    Build an "AI maintainer investigation room" for GitHub issue triage.
    Fan out 4 LLM specialists to analyze issue #${DAGSTER_ISSUE_NUM} in dagster-io/dagster
    (code, docs, reproduction, prior history), synthesize a triage decision,
    draft the final maintainer report as markdown.

    Compose ONE agentic_pipeline component (asset_name_prefix: mir).

    First step is an mcp_call to the GitHub MCP server:
      server: {name: github, type: stdio,
               command: [npx, -y, "@modelcontextprotocol/server-github"]}
      mcp_tool_name: get_issue
      tool_args: {owner: "dagster-io", repo: "dagster", issue_number: ${DAGSTER_ISSUE_NUM}}
        # NOTE: owner and repo are SEPARATE string args, and issue_number
        # MUST be an integer (not a string).
      parse_as: auto

    Every downstream llm_call and synthesize step uses \`inputs:\` field
    with typed named ports (e.g. inputs: {issue_facts: {from: fetch_issue}}).

    Use gpt-4o-mini for specialists, gpt-4o for synthesis + report.
    API key env var: OPENAI_API_KEY. GitHub token env var:
    GITHUB_PERSONAL_ACCESS_TOKEN (already exported to the parent process,
    so leave stdio server \`env\` empty — the subprocess inherits it).

    outputs.assets MUST include ALL step ids.

  include_ids: [agentic_pipeline]
  llm_model: gpt-4o
  planner_max_tokens: 8000
  loop_guard_max_failures: 6
EOF
ok "wrote the task (mir/defs.yaml)"

info "PCA planner running (one-time — this takes ~45s across 6-8 iterations)…"
uv run dg utils refresh-defs-state 2>&1 | grep -E "planned_agent|refreshed|✓" | tail -8

# --- Trim state to keep only the LARGEST successful pick ---
# PCA's iteration model adds NEW meta-component instances each iter, all
# with overlapping asset_name_prefix. Without trimming we get duplicate
# asset key errors. This is a known PCA limitation for meta-components
# (documented in the walkthrough) — the fix is to keep the last (biggest)
# successful iteration and drop earlier partials.
info "trimming state to the largest successful pick…"
python3 <<'PY'
import json, glob
state_file = glob.glob('src/'+ __import__('os').environ.get('PKG','')+'/defs/.local_defs_state/PlannedCatalogAgent__*__/state')[0] if False else None
import glob, os
# The PKG name is auto-derived — glob to find the state file.
matches = glob.glob("src/*/defs/.local_defs_state/PlannedCatalogAgent__*__/state")
if not matches:
    print("(no state file to trim)"); raise SystemExit(0)
state_file = matches[0]
d = json.load(open(state_file))
# Keep only the successful pick with the LARGEST outputs.assets list.
best = None
for p in d['plan']:
    if p.get('status') != 'success': continue
    cfg = p.get('config') or {}
    if isinstance(cfg, str): cfg = json.loads(cfg)
    n = len((cfg.get('outputs') or {}).get('assets') or [])
    if best is None or n > best[0]:
        best = (n, p)
if best is None:
    print("(no successful picks to keep)"); raise SystemExit(0)
d['plan'] = [best[1]]
json.dump(d, open(state_file, 'w'), indent=2)
print(f"kept 1 pick with {best[0]} assets")
PY

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

Or materialize the whole PCA-authored graph headless:
  DAGSTER_HOME=$DAGSTER_HOME uv run dagster asset materialize \\
    --select 'mir_*' -m $DM

The prompt lives at:
  $PROJECT_ABS/src/$PKG/defs/mir/defs.yaml

Change it + run \`dg utils refresh-defs-state\` to re-plan against a
different issue / different specialist fan-out / different report shape.
EOF
