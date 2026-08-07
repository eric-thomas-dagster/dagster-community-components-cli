#!/usr/bin/env bash
# setup_adaptive_backfill_demo.sh
#
# Adaptive Backfill Detective — the agent decides HOW to fill gaps per partition.
#
# Real IoT / metrics pipelines always have gaps: flaky sensors, network
# hiccups, upstream stalls. The dumb approach is one-size-fits-all
# ("interpolate everything" or "re-ingest everything"). The smart approach:
# an agent looks at what's actually missing PER PARTITION and picks a
# strategy from a bounded, safe set.
#
# Pipeline:
#   sparse_sensors_raw  (synthetic_data_generator, 3 sensors × 14 days × 24h,
#                        ~25% dropout — natural gaps in the data)
#         ↓
#   daily_readings_by_sensor  (summarize — group by (sensor_id, date), count
#                              readings per day, min/max readings)
#         ↓
#   backfill_plan             (langchain_chain_asset — LLM reads each
#                              (sensor, day) row and picks ONE action:
#                              ok / interpolate / re_ingest / escalate)
#         ↓
#   ┌── ok_days       ┐   (router splits by action)
#   │── interpolate_queue │
#   │── re_ingest_queue   │
#   └── escalate_queue    ┘
#         ↓ (each)
#   <action>_export.csv   (simulated per-action queues — swap for real
#                          sinks in prod: dbt-refresh trigger, Slack page,
#                          gap-filler job, etc.)
#
# The agent picks BY NAME from a bounded response set. Cannot invent
# actions. Every pick has a `reason` — auditable, reviewable, gate-able.
#
# COST: ~$0.005-$0.01 (one LLM call per (sensor, day) row, ~42 rows × gpt-4o-mini)
#
# REQUIREMENTS
#   • uv, OPENAI_API_KEY
#
# USAGE
#   export OPENAI_API_KEY=sk-...
#   ./setup_adaptive_backfill_demo.sh              # → adaptive_backfill_demo/

set -eo pipefail

PROJECT_NAME="${1:-adaptive_backfill_demo}"
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
PROJECT_ABS="$(pwd)"
mkdir -p out

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

mkdir -p "src/${PROJECT_NAME}/defs/sparse_sensors_raw" \
         "src/${PROJECT_NAME}/defs/derive_date" \
         "src/${PROJECT_NAME}/defs/daily_readings_by_sensor" \
         "src/${PROJECT_NAME}/defs/backfill_plan" \
         "src/${PROJECT_NAME}/defs/routed_plan" \
         "src/${PROJECT_NAME}/defs/ok_days_export" \
         "src/${PROJECT_NAME}/defs/interpolate_queue_export" \
         "src/${PROJECT_NAME}/defs/re_ingest_queue_export" \
         "src/${PROJECT_NAME}/defs/escalate_queue_export"

# 1. Sparse sensor readings — 3 sensors × 14 days × 24h with 25% dropout.
cat > "src/${PROJECT_NAME}/defs/sparse_sensors_raw/defs.yaml" <<'YAML'
type: dagster_community_components.SyntheticDataGeneratorComponent
attributes:
  asset_name: sparse_sensors_raw
  schema_type: sparse_sensors
  row_count: 5000
  random_state: 42
  schema_options:
    sensor_count: 3
    duration_hours: 336
    dropout_rate: 0.25
  group_name: adaptive_backfill
YAML

# 2. Derive a `date` column from the reading_ts timestamp so we can
#    group by it. Uses the formula component.
cat > "src/${PROJECT_NAME}/defs/derive_date/defs.yaml" <<'YAML'
type: dagster_community_components.FormulaComponent
attributes:
  asset_name: sensors_with_date
  upstream_asset_key: sparse_sensors_raw
  expressions:
    date: 'reading_ts.astype(str).str.slice(0, 10)'
  group_name: adaptive_backfill
YAML

# 3. Group by (sensor_id, date) and count readings. Each sensor SHOULD
#    have 24 readings per day; anything less is a gap.
cat > "src/${PROJECT_NAME}/defs/daily_readings_by_sensor/defs.yaml" <<'YAML'
type: dagster_community_components.SummarizeComponent
attributes:
  asset_name: daily_readings_by_sensor
  upstream_asset_key: sensors_with_date
  group_by:
    - sensor_id
    - date
  aggregations:
    reading_count:
      col: temperature_c
      agg: count
    avg_temp:
      col: temperature_c
      agg: mean
    min_temp:
      col: temperature_c
      agg: min
    max_temp:
      col: temperature_c
      agg: max
  group_name: adaptive_backfill
YAML

# 4. Agent picks a backfill action per (sensor, day). Bounded set:
#    ok / interpolate / re_ingest / escalate. Rules of thumb in prompt.
cat > "src/${PROJECT_NAME}/defs/backfill_plan/defs.yaml" <<'YAML'
type: dagster_community_components.LangChainChainAssetComponent
attributes:
  asset_name: backfill_plan
  upstream_asset_key: daily_readings_by_sensor
  llm_provider: openai
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  temperature: 0.1
  max_tokens: 150

  system_message: |
    You are an on-call data engineer. For each (sensor, day), a healthy
    day has 24 readings (one per hour). Given the actual reading_count,
    pick ONE action from this bounded list:

      - ok           (reading_count is 22 or higher — normal daily jitter)
      - interpolate  (reading_count is 12-21 — some hourly gaps, safe to
                      interpolate temperature between neighbors)
      - re_ingest    (reading_count is 1-11 — significant loss; if the source
                      buffers historically we should re-pull that day)
      - escalate     (reading_count is 0 — total blackout, page on-call
                      because the sensor is fully down)

    Also consider avg_temp: if it's way outside the 15-30C band the whole
    day, escalate — sensor is probably faulty.

    Output ONLY JSON, no markdown fences. Keys:
      action  (string, from list above)
      reason  (short — WHY this action given the numbers)

  prompt_template: |
    Sensor: {sensor_id}
    Date: {date}
    Reading count: {reading_count}  (expected: 24)
    Avg temp: {avg_temp}
    Min temp: {min_temp}
    Max temp: {max_temp}

  response_column: action_json
  parse_json: true
  group_name: adaptive_backfill
YAML

# 5. Router splits by action.
cat > "src/${PROJECT_NAME}/defs/routed_plan/defs.yaml" <<'YAML'
type: dagster_community_components.RouterComponent
attributes:
  upstream_asset_key: backfill_plan
  routes:
    - asset_name: ok_days
      condition: 'action == "ok"'
    - asset_name: interpolate_queue
      condition: 'action == "interpolate"'
    - asset_name: re_ingest_queue
      condition: 'action == "re_ingest"'
  default_asset_name: escalate_queue
  exclusive: true
  group_name: adaptive_backfill
YAML

# 6. Per-action CSV sinks — simulated. In prod, replace with:
#     ok_days                → no downstream (drop)
#     interpolate_queue      → a dagster job that runs interpolation
#     re_ingest_queue        → a job that re-pulls that (sensor, day) from source
#     escalate_queue         → slack_notification / pagerduty_alert
for route in ok_days interpolate_queue re_ingest_queue escalate_queue; do
  cat > "src/${PROJECT_NAME}/defs/${route}_export/defs.yaml" <<YAML
type: dagster_community_components.DataframeToCsvComponent
attributes:
  asset_name: ${route}_export
  upstream_asset_key: ${route}
  file_path: out/${PROJECT_NAME}/${route}.csv
  group_name: adaptive_backfill
YAML
done

ok "Wrote 9 defs.yaml (raw + date + group + plan + router + 4 sinks)"

DM="${PROJECT_NAME}.definitions"

info "Materializing sparse_sensors_raw (3 sensors × 14 days, 25% dropout)…"
uv run dagster asset materialize --select sparse_sensors_raw -m "$DM" 2>&1 | tail -3 || fail "raw failed"

info "Deriving date column…"
uv run dagster asset materialize --select sensors_with_date -m "$DM" 2>&1 | tail -3 || fail "date failed"

info "Grouping per (sensor, day)…"
uv run dagster asset materialize --select daily_readings_by_sensor -m "$DM" 2>&1 | tail -3 || fail "summarize failed"

info "Agent picking backfill action per (sensor, day) — gpt-4o-mini…"
uv run dagster asset materialize --select backfill_plan -m "$DM" 2>&1 | tail -3 || fail "plan failed"

info "Routing + writing per-action CSV sinks (one run)…"
uv run dagster asset materialize --select 'backfill_plan++' -m "$DM" 2>&1 | tail -3 || fail "route/sink failed"

echo
ok "Demo complete."
echo
info "Per-action (sensor, day) counts:"
for route in ok_days interpolate_queue re_ingest_queue escalate_queue; do
  f="$PROJECT_ABS/out/${PROJECT_NAME}/${route}.csv"
  if [ -f "$f" ]; then
    lines=$(wc -l < "$f" | tr -d ' ')
    n=$((lines - 1))
    echo "  ${route}: ${n} → ${f}"
  else
    echo "  ${route}: 0 (empty)"
  fi
done
echo
cat <<EOF

Inspect the audit trail:
  cd $PROJECT_NAME
  uv run dg dev
    → asset graph: raw → date-derive → group → plan → 4 queues → 4 exports
    → click backfill_plan → see the agent's action + reason per (sensor, day)
    → the LLM correctly separates the "just noise" (ok) days from
      "needs interpolation" from "we need to re-pull" from "escalate now"

In prod, replace the 4 export CSVs with the actual responses:
  ok_days           → drop (no work needed)
  interpolate_queue → run gap-fill job with per-row date param
  re_ingest_queue   → trigger a re-ingest job for that (sensor, day)
  escalate_queue    → slack_notification / pagerduty_alert
EOF
