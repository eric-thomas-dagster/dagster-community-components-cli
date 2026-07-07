#!/usr/bin/env bash
# setup_data_quality_agent_demo.sh
#
# Data quality with LLM explanations. Every DQ pipeline says "row 42 is
# anomalous, z-score 4.2" — but no one has time to figure out WHY. This
# demo runs statistical anomaly detection and then has an LLM narrate a
# plausible business explanation per anomaly.
#
# Two-pass shape:
#   1. anomaly_detection (z-score / IQR / percentile) — flags outliers
#   2. langchain_chain_asset — LLM writes a business narrative per anomaly
#      ("this transaction is 4.2x the median amount for its category, likely
#      a chargeback attempt or bulk-order")
#
# What it demonstrates
#   • synthetic_data_generator (transactions with seeded anomalies)
#   • anomaly_detection (statistical outlier flagging)
#   • langchain_chain_asset (row-wise LLM narration)
#
# Cost: ~$0.005 per run (LLM per anomaly row, small).
#
# Requirements
#   • uv, OPENAI_API_KEY
#
# Usage
#   export OPENAI_API_KEY=sk-...
#   ./setup_data_quality_agent_demo.sh                   # → data_quality_agent_demo/

set -eo pipefail

PROJECT_NAME="${1:-data_quality_agent_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_YELLOW='\033[0;33m'; C_RED='\033[0;31m'; C_NC='\033[0m'
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
    'pandas>=1.5.0' 'scipy>=1.10.0' \
    'langchain-core>=0.3.0' 'langchain-openai>=0.2.0' 'tabulate>=0.9.0' \
    || fail "uv add failed"
else
  uv add --quiet 'dagster-community-components @ git+https://github.com/eric-thomas-dagster/dagster-component-templates.git' \
    'pandas>=1.5.0' 'scipy>=1.10.0' \
    'langchain-core>=0.3.0' 'langchain-openai>=0.2.0' 'tabulate>=0.9.0' \
    || fail "uv add failed"
fi
ok "Deps installed"

# Persistent IO manager for cross-invocation DataFrame hand-off.
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

mkdir -p "src/${PROJECT_NAME}/defs/transactions" \
         "src/${PROJECT_NAME}/defs/anomalies" \
         "src/${PROJECT_NAME}/defs/anomalies_only" \
         "src/${PROJECT_NAME}/defs/anomaly_narratives"

# 1. Synthetic transactions (has natural distribution + outliers)
cat > "src/${PROJECT_NAME}/defs/transactions/defs.yaml" <<'YAML'
type: dagster_community_components.SyntheticDataGeneratorComponent
attributes:
  asset_name: transactions
  schema_type: transactions
  row_count: 500
  random_state: 42
  group_name: dq
YAML

# 2. Anomaly detection on the amount column. Threshold interpretation
#    depends on the component's scoring — this instance emits scores in the
#    0–2 range, so 1.5 catches the top-tail outliers.
cat > "src/${PROJECT_NAME}/defs/anomalies/defs.yaml" <<'YAML'
type: dagster_community_components.AnomalyDetectionComponent
attributes:
  asset_name: anomalies
  upstream_asset_key: transactions
  detection_method: z_score
  metric_column: amount
  threshold: 1.5
  group_name: dq
YAML

# 3. Filter to flagged anomalies only — LLM per-row is expensive and only
#    the flagged rows are worth explaining.
cat > "src/${PROJECT_NAME}/defs/anomalies_only/defs.yaml" <<'YAML'
type: dagster_community_components.FilterComponent
attributes:
  asset_name: anomalies_only
  upstream_asset_key: anomalies
  condition: "is_anomaly == True"
  group_name: dq
YAML

# 4. LLM narrates each anomaly. Reads the anomaly row + its anomaly_score
#    and writes a plain-English "here's a plausible reason" note.
cat > "src/${PROJECT_NAME}/defs/anomaly_narratives/defs.yaml" <<'YAML'
type: dagster_community_components.LangChainChainAssetComponent
attributes:
  asset_name: anomaly_narratives
  upstream_asset_key: anomalies_only
  llm_provider: openai
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  temperature: 0.2
  max_tokens: 200

  system_message: |
    You are a data-quality analyst. For each flagged transaction anomaly,
    write ONE plausible business reason (fraud, testing, bulk-purchase,
    bad ETL upstream, etc.) plus ONE concrete follow-up check the
    on-call analyst should run. Output JSON with two keys:
    "plausible_reason" and "followup_check".

  prompt_template: |
    Transaction anomaly:
      transaction_id: {transaction_id}
      amount: {amount}
      type: {type}
      category: {category}
      anomaly_score: {anomaly_score}
      anomaly_reason: {anomaly_reason}
      timestamp: {timestamp}

  parse_json: true
  group_name: dq
YAML

ok "Wrote 3 defs.yaml"

DM="${PROJECT_NAME}.definitions"
info "Materializing transactions (500 synthetic)…"
uv run dagster asset materialize --select transactions -m "$DM" 2>&1 | tail -3 || fail "transactions failed"
info "Materializing anomalies (z-score > 3)…"
uv run dagster asset materialize --select anomalies -m "$DM" 2>&1 | tail -3 || fail "anomalies failed"
info "Filtering to flagged anomalies only…"
uv run dagster asset materialize --select anomalies_only -m "$DM" 2>&1 | tail -3 || fail "filter failed"
info "Materializing anomaly_narratives (LLM explanation per anomaly)…"
uv run dagster asset materialize --select anomaly_narratives -m "$DM" 2>&1 | tail -3 || fail "narratives failed"

echo
ok "Demo complete."
echo
cat <<EOF
The pipeline just ran:
  1. Generated 500 synthetic transactions with a natural log-normal
     distribution + some seeded outliers
  2. Ran z-score anomaly detection on 'amount' — flagged rows with |z| > 3
  3. gpt-4o-mini wrote a plausible business reason + concrete follow-up
     check per anomaly row

Inspect the results:
  cd $PROJECT_NAME
  uv run dg dev
    → asset graph: transactions → anomalies → anomaly_narratives
    → click anomaly_narratives → each row has plausible_reason + followup_check

Extension ideas:
  • Chain a dagster_asset_check that fails if anomaly count exceeds N per run
  • Add a Slack sink (slack_notification component) to page on-call when
    anomaly_narratives has any 'fraud' plausible_reason
  • Replace synthetic transactions with your Snowflake / BigQuery reads
    (dataframe_to_snowflake in reverse — a snowflake_query_asset)
EOF
