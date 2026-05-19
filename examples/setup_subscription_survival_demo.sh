#!/usr/bin/env bash
# Subscription survival demo — Kaplan-Meier on synthetic SaaS data.
#
# Generates 300 synthetic subscriptions (free/pro/enterprise tiers with
# realistic churn rates) via synthetic_data_generator's `subscriptions`
# schema, then fits Kaplan-Meier survival curves grouped by plan_tier.
#
# Pipeline (3 components, all autoloaded by `dg`):
#     synthetic_data_generator → survival_analysis → dataframe_to_csv

set -euo pipefail

PROJECT_DIR="${1:-subscription-survival-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas lifelines
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 3 community components into src/$PKG/components/ + defs/"
$CLI add synthetic_data_generator --auto-install
$CLI add survival_analysis        --auto-install
$CLI add dataframe_to_csv         --auto-install
$CLI add cron_schedule         --auto-install

echo ">>> Writing demo defs.yaml for each component"

cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: subscription_cohort
  schema_type: subscriptions
  row_count: 300
  random_state: 42
  description: 300 synthetic SaaS subscriptions across free/pro/enterprise
  group_name: ingest
EOF

cat > "src/$PKG/defs/survival_analysis/defs.yaml" <<EOF
type: $PKG.components.survival_analysis.component.SurvivalAnalysisComponent
attributes:
  asset_name: subscription_survival_curves
  upstream_asset_key: subscription_cohort
  duration_column: days_active
  event_column: cancelled
  group_column: plan_tier
  method: kaplan_meier
  time_points: [30, 60, 90, 180, 365]
  group_name: analysis
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: survival_report
  upstream_asset_key: subscription_survival_curves
  file_path: /tmp/subscription_survival.csv
  include_index: false
  group_name: sink
EOF

cat > "src/$PKG/defs/cron_schedule/defs.yaml" <<EOF
type: $PKG.components.cron_schedule.component.CronScheduleComponent
attributes:
  schedule_name: weekly_survival_refresh
  cron_expression: "0 8 * * 1"
  asset_keys:
    - survival_report
  default_status: STOPPED
  tags:
    purpose: survival_refresh
EOF

cat <<MSG

>>> Setup complete.

Materialize headlessly:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Or open the UI:
    cd $PROJECT_DIR && uv run dg dev

Output: /tmp/subscription_survival.csv — survival probability at days
30, 60, 90, 180, 365 for each plan tier.

Inspect — what fraction of free-tier subs are still active after 60 days?
    cat /tmp/subscription_survival.csv
MSG
