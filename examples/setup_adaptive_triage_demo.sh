#!/usr/bin/env bash
# setup_adaptive_triage_demo.sh
#
# Adaptive Triage Router — the agent picks WHICH downstream runs per row.
#
# Classic ETL routes rows on hard-coded predicates. Adaptive triage puts
# an LLM in front of the router: it classifies each row into one of N
# pre-defined routes based on the row's content, and the router
# fan-outs to different downstream assets by route.
#
# Real-world shape: incoming support tickets → LLM classifies into
# billing / bug / churn_risk / spam / other → each queue goes to a
# different destination (billing team, engineering, save-the-customer
# playbook, spam sink, catch-all).
#
# Pipeline:
#   raw_tickets           (synthetic_data_generator, support_tickets schema)
#         ↓
#   classified_tickets    (langchain_chain_asset — LLM adds a `route`
#                          column + confidence + reason per row)
#         ↓
#   ┌── billing_queue     ┐   (each = router output, filtered by
#   │── bug_queue         │    `route == "..."`)
#   │── churn_risk_queue  │
#   │── spam_queue        │
#   └── other_queue       ┘
#         ↓ (each)
#   <route>_export.csv    (simulated sinks — real deployments swap
#                          for slack_notification / jira_ticket /
#                          crm_lead / dead_letter / etc.)
#
# The agent picks BY NAME from a bounded route set. No arbitrary
# code. Every classification's confidence + reason lands in the
# classified_tickets asset for audit.
#
# COST: ~$0.005/run (20 tickets × gpt-4o-mini)
#
# REQUIREMENTS
#   • uv, OPENAI_API_KEY
#
# USAGE
#   export OPENAI_API_KEY=sk-...
#   ./setup_adaptive_triage_demo.sh                 # → adaptive_triage_demo/

set -eo pipefail

PROJECT_NAME="${1:-adaptive_triage_demo}"
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
    'pandas>=1.5.0' 'tabulate>=0.9.0' \
    'langchain-core>=0.3.0' 'langchain-openai>=0.2.0' \
    || fail "uv add failed"
else
  uv add --quiet 'dagster-community-components @ git+https://github.com/eric-thomas-dagster/dagster-component-templates.git' \
    'pandas>=1.5.0' 'tabulate>=0.9.0' \
    'langchain-core>=0.3.0' 'langchain-openai>=0.2.0' \
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

mkdir -p "src/${PROJECT_NAME}/defs/raw_tickets" \
         "src/${PROJECT_NAME}/defs/classified_tickets" \
         "src/${PROJECT_NAME}/defs/routed_tickets" \
         "src/${PROJECT_NAME}/defs/billing_export" \
         "src/${PROJECT_NAME}/defs/bug_export" \
         "src/${PROJECT_NAME}/defs/churn_risk_export" \
         "src/${PROJECT_NAME}/defs/spam_export" \
         "src/${PROJECT_NAME}/defs/other_export"

# 1. Synthetic support tickets — mixed content the LLM will classify.
cat > "src/${PROJECT_NAME}/defs/raw_tickets/defs.yaml" <<'YAML'
type: dagster_community_components.SyntheticDataGeneratorComponent
attributes:
  asset_name: raw_tickets
  schema_type: support_tickets
  row_count: 20
  random_state: 42
  group_name: adaptive_triage
YAML

# 2. Agent classifies each ticket. Emits {route, confidence, reason}
#    which parse_json expands into per-row columns on the DataFrame.
cat > "src/${PROJECT_NAME}/defs/classified_tickets/defs.yaml" <<'YAML'
type: dagster_community_components.LangChainChainAssetComponent
attributes:
  asset_name: classified_tickets
  upstream_asset_key: raw_tickets
  llm_provider: openai
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  temperature: 0.1
  max_tokens: 200

  system_message: |
    You are a support-ticket triage router. Read one ticket and classify it
    into EXACTLY ONE of these routes (pick the best fit):

      - billing        (invoice / payment / refund / subscription / pricing questions)
      - bug            (something broken, error message, feature not working)
      - churn_risk     (customer expressing frustration, cancellation, or leaving)
      - spam           (promotional, off-topic, obvious junk, non-support)
      - other          (anything else — questions, docs, general inquiry)

    Output ONLY JSON (no markdown fences). Keys:
      route       (string, from the list above)
      confidence  (float 0-1)
      reason      (short string, why this route)

  prompt_template: |
    Ticket ID: {ticket_id}
    Channel: {channel}
    Priority: {priority}
    Body: {ticket_text}

  response_column: classification_json
  parse_json: true
  group_name: adaptive_triage
YAML

# 3. Router splits classified_tickets into 5 per-route DataFrames.
#    Each downstream asset receives ONLY the rows the LLM tagged for it.
cat > "src/${PROJECT_NAME}/defs/routed_tickets/defs.yaml" <<'YAML'
type: dagster_community_components.RouterComponent
attributes:
  upstream_asset_key: classified_tickets
  routes:
    - asset_name: billing_queue
      condition: 'route == "billing"'
    - asset_name: bug_queue
      condition: 'route == "bug"'
    - asset_name: churn_risk_queue
      condition: 'route == "churn_risk"'
    - asset_name: spam_queue
      condition: 'route == "spam"'
  default_asset_name: other_queue
  exclusive: true
  group_name: adaptive_triage
YAML

# 4. Simulated sinks — one CSV per route. In production, swap each for
#    a real destination: slack_notification / jira_ticket / crm_lead /
#    dead_letter / etc.
for route in billing bug churn_risk spam other; do
  cat > "src/${PROJECT_NAME}/defs/${route}_export/defs.yaml" <<YAML
type: dagster_community_components.DataframeToCsvComponent
attributes:
  asset_name: ${route}_export
  upstream_asset_key: ${route}_queue
  file_path: /tmp/${PROJECT_NAME}/${route}.csv
  group_name: adaptive_triage
YAML
done

ok "Wrote 8 defs.yaml (1 source + 1 classifier + 1 router + 5 sinks)"

DM="${PROJECT_NAME}.definitions"

info "Materializing raw_tickets…"
uv run dagster asset materialize --select raw_tickets -m "$DM" 2>&1 | tail -3 || fail "raw failed"

info "Agent classifying (gpt-4o-mini per row)…"
uv run dagster asset materialize --select classified_tickets -m "$DM" 2>&1 | tail -3 || fail "classify failed"

info "Routing to per-route queues + writing per-route CSV sinks (all in one run so IO manager sees the plan output)…"
uv run dagster asset materialize --select 'classified_tickets++' -m "$DM" 2>&1 | tail -3 || fail "route/sink failed"

echo
ok "Demo complete."
echo
info "Per-route ticket counts:"
for route in billing bug churn_risk spam other; do
  f="/tmp/${PROJECT_NAME}/${route}.csv"
  if [ -f "$f" ]; then
    lines=$(wc -l < "$f" | tr -d ' ')
    n=$((lines - 1))
    echo "  ${route}: ${n} tickets → ${f}"
  else
    echo "  ${route}: 0 tickets (no CSV written — empty route)"
  fi
done
echo
cat <<EOF

Inspect the audit trail:
  cd $PROJECT_NAME
  uv run dg dev
    → asset graph: classified_tickets fans out to 5 queues → 5 exports
    → click classified_tickets → see the agent's route + confidence + reason per ticket
    → each per-route queue only has the rows the LLM tagged for it

The pattern: LLM decides WHICH downstream, Dagster fan-outs
declaratively. In production, replace the CSV exports with real
sinks (Slack for churn_risk, JIRA for bug, CRM for billing…).
EOF
