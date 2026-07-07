#!/usr/bin/env bash
# setup_data_doctor_demo.sh
#
# Data Doctor — the agent WRITES the pipeline plan.
#
# Every DQ pipeline hard-codes what to do about issues: "if nulls > 3%,
# drop; if outliers detected, clip at z=3." What if the AGENT decides,
# based on the actual data it sees, which remediation makes sense per
# column? That's this demo.
#
# Pipeline:
#   raw_transactions  (synthetic + intentionally seeded DQ issues)
#         ↓
#   dq_profile       (dataframe_describe — one row per column with stats)
#         ↓
#   remediation_plan (langchain_chain_asset — LLM reads each column's
#                     stats and picks ONE action from a bounded list.
#                     Emits {action, params, reason} per row.)
#         ↓
#   cleaned_transactions (data_remediation_asset — applies the LLM's
#                         chosen actions from a fixed safe action space)
#         ↓
#   verification_profile (dataframe_describe — proves the issues are fixed)
#
# Why this is safer than "LLM writes SQL/code": the agent picks actions
# BY NAME from a bounded set. It can't invent code, can't run unsafe ops.
# Every action is auditable — the plan DataFrame is the audit log.
#
# Bounded action set (data_remediation_asset):
#   none / drop_nulls / fill_nulls / fill_nulls_with_median / fill_nulls_with_mean /
#   fill_nulls_with_mode / cast_type / dedup / clip_outliers / filter_range /
#   strip_whitespace
#
# COST: ~$0.005/run (one LLM call per profile row, ~10 rows × gpt-4o-mini)
#
# REQUIREMENTS
#   • uv, OPENAI_API_KEY
#
# USAGE
#   export OPENAI_API_KEY=sk-...
#   ./setup_data_doctor_demo.sh                     # → data_doctor_demo/

set -eo pipefail

PROJECT_NAME="${1:-data_doctor_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_YELLOW='\033[0;33m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
warn()  { echo -e "${C_YELLOW}!${C_NC} $*"; }
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
    'pandas>=1.5.0' 'numpy>=1.24.0' 'tabulate>=0.9.0' \
    'langchain-core>=0.3.0' 'langchain-openai>=0.2.0' \
    || fail "uv add failed"
else
  uv add --quiet 'dagster-community-components @ git+https://github.com/eric-thomas-dagster/dagster-component-templates.git' \
    'pandas>=1.5.0' 'numpy>=1.24.0' 'tabulate>=0.9.0' \
    'langchain-core>=0.3.0' 'langchain-openai>=0.2.0' \
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

mkdir -p "src/${PROJECT_NAME}/defs/raw_transactions" \
         "src/${PROJECT_NAME}/defs/dq_profile" \
         "src/${PROJECT_NAME}/defs/remediation_plan" \
         "src/${PROJECT_NAME}/defs/cleaned_transactions" \
         "src/${PROJECT_NAME}/defs/verification_profile"

# 1. Raw transactions with intentional DQ issues injected.
cat > "src/${PROJECT_NAME}/defs/raw_transactions/defs.yaml" <<'YAML'
type: dagster_community_components.SyntheticDataGeneratorComponent
attributes:
  asset_name: raw_transactions
  schema_type: transactions
  row_count: 300
  random_state: 42
  inject_dq_issues: true
  group_name: data_doctor
YAML

# 2. Profile every column — this is what the LLM will diagnose.
cat > "src/${PROJECT_NAME}/defs/dq_profile/defs.yaml" <<'YAML'
type: dagster_community_components.DataframeDescribeComponent
attributes:
  asset_name: dq_profile
  upstream_asset_key: raw_transactions
  include: all
  include_dtypes_column: true
  include_null_pct: true
  group_name: data_doctor
YAML

# 3. LLM reads each profile row (one per column) and picks ONE action
#    from the bounded set. Emits JSON that langchain_chain_asset expands
#    into columns via parse_json.
cat > "src/${PROJECT_NAME}/defs/remediation_plan/defs.yaml" <<'YAML'
type: dagster_community_components.LangChainChainAssetComponent
attributes:
  asset_name: remediation_plan
  upstream_asset_key: dq_profile
  llm_provider: openai
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  temperature: 0.1
  max_tokens: 300

  system_message: |
    You are a data-quality analyst. For ONE column's descriptive statistics,
    pick ONE action from this bounded list:

      - none                        (no issues detected, pass through)
      - drop_nulls                  (drop rows with null; use if null_pct < 2)
      - fill_nulls_with_median      (numeric, null_pct > 3)
      - fill_nulls_with_mode        (categorical, null_pct > 3)
      - clip_outliers               (numeric, when max is very large vs mean+std; use params z_max=3)
      - strip_whitespace            (string cols only)
      - dedup                       (rare — only if col is a key)
      - cast_type                   (if dtype is wrong; use params dtype=<target>)

    Rules of thumb:
      - null_pct less than 1 and numeric — drop_nulls is safe
      - null_pct 1-10 numeric — fill_nulls_with_median
      - max is more than 5 standard deviations from mean — clip_outliers with z_max=3
      - object dtype — consider strip_whitespace
      - Otherwise pick "none"

    Output ONLY JSON, no markdown fences. Keys:
      action   (string, from list above)
      params   (object; use double curly braces around JSON keys in your response)
      reason   (short string, WHY you picked this action given the stats)

    Do NOT include a "column" key — the upstream row already carries it.

  prompt_template: |
    Column: {column}
    dtype: {dtype}
    count: {count}
    null_pct: {null_pct}
    mean: {mean}
    std: {std}
    min: {min}
    max: {max}

  response_column: action_json
  parse_json: true
  group_name: data_doctor
YAML

# 4. Apply the agent's plan. The plan_key output has ALL the profile
#    columns + the LLM-added action/params/reason columns; the
#    remediation component only reads column/action/params/reason.
cat > "src/${PROJECT_NAME}/defs/cleaned_transactions/defs.yaml" <<'YAML'
type: dagster_community_components.DataRemediationAssetComponent
attributes:
  asset_name: cleaned_transactions
  upstream_data_key: raw_transactions
  plan_key: remediation_plan
  fail_on_unknown_action: false
  group_name: data_doctor
YAML

# 5. Verification — re-profile the cleaned data. Compare to dq_profile
#    to prove the issues are actually gone.
cat > "src/${PROJECT_NAME}/defs/verification_profile/defs.yaml" <<'YAML'
type: dagster_community_components.DataframeDescribeComponent
attributes:
  asset_name: verification_profile
  upstream_asset_key: cleaned_transactions
  include: all
  include_dtypes_column: true
  include_null_pct: true
  group_name: data_doctor
YAML

ok "Wrote 5 defs.yaml"

DM="${PROJECT_NAME}.definitions"

info "Materializing raw_transactions (300 rows + injected DQ issues)…"
uv run dagster asset materialize --select raw_transactions -m "$DM" 2>&1 | tail -3 || fail "raw failed"

info "Profiling raw_transactions…"
uv run dagster asset materialize --select dq_profile -m "$DM" 2>&1 | tail -3 || fail "profile failed"

info "Agent picking remediation actions (gpt-4o-mini per column)…"
uv run dagster asset materialize --select remediation_plan -m "$DM" 2>&1 | tail -3 || fail "plan failed"

info "Applying the agent's plan…"
uv run dagster asset materialize --select cleaned_transactions -m "$DM" 2>&1 | tail -3 || fail "clean failed"

info "Verifying the cleaned data…"
uv run dagster asset materialize --select verification_profile -m "$DM" 2>&1 | tail -3 || fail "verify failed"

echo
ok "Demo complete."
echo
cat <<EOF
The pipeline just ran end-to-end. The AGENT decided WHAT the pipeline does:
  1. Seeded 300 synthetic transactions with intentional DQ issues
     (nulls, outliers, whitespace, duplicate rows)
  2. Profiled every column (dataframe_describe)
  3. gpt-4o-mini looked at each column's stats and picked ONE action
     from the bounded safe list, with a reason
  4. data_remediation_asset APPLIED the picked actions
  5. Re-profiled the cleaned data — issues gone

Inspect the audit trail:
  cd $PROJECT_NAME
  uv run dg dev
    → asset graph
    → click remediation_plan → see the agent's picks + reasons per column
    → click cleaned_transactions → see plan_summary + rows before/after

The key primitive: data_remediation_asset takes the LLM's plan and
executes from a BOUNDED action space. The agent picks by name, cannot
write arbitrary code. Every action is logged with a reason → full
audit trail.
EOF
