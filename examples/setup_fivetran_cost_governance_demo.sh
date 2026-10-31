#!/usr/bin/env bash
# setup_fivetran_cost_governance_demo.sh
#
# Catch runaway Fivetran connector costs before the bill arrives, not after.
# Chains FivetranUsageObservationSensorComponent (observes Monthly Active Rows
# per synced table from Fivetran's Platform Connector) with a budget-threshold
# check and SlackMessageSendComponent (dry-run alert).
#
#   fivetran_usage_observer (sensor, demo_mode)
#            |  (emits AssetObservation deltas per table)
#            v
#   over_budget_fivetran_tables → fivetran_budget_alert
#     (budget-threshold glue)      (Slack, dry_run)
#
# IMPORTANT -- sensors run on the Dagster DAEMON (`dg dev`), not via
# `dg launch`, so this script verifies the sensor's real tick logic directly
# via `dagster.build_sensor_context()` (the exact technique this component's
# own test suite uses) rather than requiring a live daemon process. The
# budget-check → Slack-alert half is then demoed as a real one-shot
# `dg launch`, using the SAME synthetic MAR numbers the sensor's own
# demo_mode produces at tick 0 (hardcoded in a small glue asset -- no generic
# "read an observation and alert" component exists in the registry, so this
# step is ordinary Dagster user code, not a new component). In production
# this glue asset would instead read
# `context.instance.get_latest_observation_event(asset_key)`, since the
# sensor runs continuously via the daemon and there would be real
# observation history to read by the time this asset runs.
#
# What it demonstrates
#   • fivetran_usage_observation_sensor (demo_mode) — reports MAR as a DELTA,
#     not a cumulative level (correct for Dagster+ Insights custom metrics,
#     which sum whatever's reported under a key), and skips emitting anything
#     for a table whose MAR didn't change since the last tick
#   • a small budget-threshold glue asset flagging tables over a row budget
#   • slack_message_send (dry_run: true) — the alert sink
#
# Cost: $0 — demo_mode: true on the sensor, dry_run: true on the Slack step.
# No Fivetran account, no warehouse, no Slack workspace needed.
#
# Requirements
#   • uv (https://docs.astral.sh/uv/)
#
# Usage
#   ./setup_fivetran_cost_governance_demo.sh                  # → fivetran_cost_governance_demo/
#   ./setup_fivetran_cost_governance_demo.sh my_project        # custom name

set -eo pipefail

PROJECT_NAME="${1:-fivetran_cost_governance_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

command -v uvx >/dev/null 2>&1 || fail "uvx not found."
[ -d "$PROJECT_DIR" ] && fail "Directory exists: $PROJECT_DIR"

info "Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

info "Installing deps (pandas)…"
uv add -q pandas
uv add --dev -q dagster-dg-cli dagster-webserver

info "Installing components (--refresh on first call busts the registry-manifest cache)"
uvx --from dagster-community-components-cli dagster-component --refresh add fivetran_usage_observation_sensor --auto-install
uvx --from dagster-community-components-cli dagster-component add slack_message_send --auto-install

info "Writing defs.yaml (sensor + budget glue asset + Slack alert)"

cat > "src/$PKG/defs/fivetran_usage_observation_sensor/defs.yaml" <<EOF
type: $PKG.components.fivetran_usage_observation_sensor.component.FivetranUsageObservationSensorComponent
attributes:
  sensor_name: fivetran_usage_observer
  resource_key: unused_in_demo_mode
  demo_mode: true
  check_interval_seconds: 3600
EOF

mkdir -p "src/$PKG/defs/budget_guardrail"
cat > "src/$PKG/defs/budget_guardrail/definitions.py" <<'PYEOF'
"""Budget-threshold glue: no generic "read an asset observation and alert"
component exists in the registry, so this is an ordinary Dagster asset (not
a new reusable component) reading the SAME synthetic MAR numbers
fivetran_usage_observation_sensor's own demo_mode (`_demo_rows(tick=0)`)
returns, and flagging any table whose month-to-date MAR exceeds a budget.

In production this would instead read
`context.instance.get_latest_observation_event(asset_key)` for each table the
sensor tracks -- the sensor runs continuously via the Dagster daemon, so by
the time this asset runs there would be real observation history to read.
This demo hardcodes the sensor's own tick-0 demo values so the whole chain
is verifiable in one `dg launch` without needing a live daemon process.
"""
import dagster as dg
import pandas as pd

BUDGET_MAR_ROWS = 2000  # demo threshold -- postgres_orders.customers (4200) is over it, the other two are under


@dg.asset(group_name="monitoring")
def over_budget_fivetran_tables() -> pd.DataFrame:
    # Matches fivetran_usage_observation_sensor's own _demo_rows(tick=0) exactly.
    rows = [
        {"schema_name": "postgres_orders", "table_name": "orders", "mar_month_to_date": 1000},
        {"schema_name": "postgres_orders", "table_name": "customers", "mar_month_to_date": 4200},
        {"schema_name": "salesforce", "table_name": "opportunity", "mar_month_to_date": 300},
    ]
    df = pd.DataFrame(rows)
    over = df[df["mar_month_to_date"] > BUDGET_MAR_ROWS].copy()
    over["budget_mar_rows"] = BUDGET_MAR_ROWS
    over["table_name_full"] = over["schema_name"] + "." + over["table_name"]
    return over


defs = dg.Definitions(assets=[over_budget_fivetran_tables])
PYEOF

cat > "src/$PKG/defs/slack_message_send/defs.yaml" <<EOF
type: $PKG.components.slack_message_send.component.SlackMessageSendComponent
attributes:
  asset_name: fivetran_budget_alert
  upstream_asset_key: over_budget_fivetran_tables
  token_env_var: SLACK_BOT_TOKEN
  mode: per_row
  channel_template: "#data-platform-costs"
  message_template: |
    :rotating_light: Fivetran table *{table_name_full}* is over its MAR budget this month.
    Month-to-date MAR: {mar_month_to_date} rows (budget: {budget_mar_rows})
  group_name: monitoring
  dry_run: true
EOF

ok "Wrote 3 defs.yaml/.py files"

export SLACK_BOT_TOKEN="xoxb-demo-dry-run-placeholder"

info "Verifying the project loads"
uv run dg check defs 2>&1 | tail -5

info "Verifying the sensor's real tick logic directly (2 ticks, via dagster.build_sensor_context -- the same technique this component's own test suite uses)"
uv run python3 - <<'PYEOF'
import importlib.util
import dagster as dg

spec = importlib.util.spec_from_file_location(
    "fivetran_sensor_component",
    next(p for p in __import__("pathlib").Path("src").glob("*/components/fivetran_usage_observation_sensor/component.py")),
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

comp = mod.FivetranUsageObservationSensorComponent(sensor_name="fivetran_usage_observer", resource_key="unused", demo_mode=True)
sensor_def = comp.build_defs(None).sensors[0]

ctx1 = dg.build_sensor_context()
result1 = sensor_def(ctx1)
print("TICK 1 (every table reports its initial total as a delta):")
for e in result1.asset_events:
    print(f"  {e.asset_key.to_user_string():35s} delta={e.metadata['fivetran/mar'].value:>5}  month-to-date={e.metadata['fivetran/mar_month_to_date'].value}")

ctx2 = dg.build_sensor_context(cursor=result1.cursor)
result2 = sensor_def(ctx2)
print("TICK 2 (only CHANGED tables emit; postgres_orders.customers is unchanged -> correctly skipped):")
for e in result2.asset_events:
    print(f"  {e.asset_key.to_user_string():35s} delta={e.metadata['fivetran/mar'].value:>5}  month-to-date={e.metadata['fivetran/mar_month_to_date'].value}")
PYEOF

info "Materializing the budget-check → Slack-alert chain"
uv run dg launch --assets 'over_budget_fivetran_tables,fivetran_budget_alert' 2>&1 | grep -E "DRY-RUN|RUN_SUCCESS|RUN_FAILURE"

echo
ok "Demo complete."
echo
cat <<EOF
What just happened:
  1. The sensor's real tick logic ran twice via Dagster's own sensor-testing
     API -- tick 1 reported all 3 tables' initial totals; tick 2 correctly
     reported ONLY the two tables whose MAR actually changed (deltas, not
     levels), and silently skipped the unchanged one -- exactly the behavior
     documented in the component's module docstring.
  2. A budget-threshold check flagged postgres_orders.customers (4200 rows,
     over the 2000-row demo budget) and sent a DRY-RUN Slack alert for it.

Inspect the results:
  cd $PROJECT_NAME
  uv run dg dev
    → click over_budget_fivetran_tables / fivetran_budget_alert

Point at a real Fivetran + warehouse account:
  Set demo_mode: false, resource_key to a real Dagster resource exposing
  .get_connection() against the warehouse your Fivetran Platform Connector
  writes to, and mar_table to your real fully-qualified INCREMENTAL_MAR
  table name. Start the Dagster daemon (dg dev, or a deployed schedule/sensor
  daemon) so the sensor actually ticks on check_interval_seconds.

Point at a real Slack workspace:
  Set dry_run: false and a real SLACK_BOT_TOKEN -- channel_template/
  message_template are unchanged.

Production variants:
  • Replace the budget_guardrail glue asset with one reading
    context.instance.get_latest_observation_event(asset_key) for your real
    tracked tables instead of hardcoded demo numbers
  • Register fivetran/mar as a Dagster+ Insights custom metric to chart
    MAR cost over time across every synced table
EOF
