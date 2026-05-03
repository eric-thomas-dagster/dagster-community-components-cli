#!/usr/bin/env bash
# Retail Customer Analytics demo — multi-component end-to-end pipeline.
#
# Generates 2000 synthetic orders, parses the dates, then runs TWO
# parallel analytics branches off the typed dataset: RFM segmentation
# (recency/frequency/monetary scoring) and monthly cohort retention.
# Each branch sinks to its own CSV, plus a running-total view of
# cumulative spend per customer.
#
# Pipeline (7 components, all autoloaded by `dg`):
#                              ┌─→ rfm_segmentation     → segments_csv
#                              │
#     synthetic_orders          ├─→ cohort_analysis      → cohorts_csv
#       └─→ datetime_parser ───┤
#                              └─→ running_total         → spend_csv

set -euo pipefail

PROJECT_DIR="${1:-retail-analytics-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 7 community components into src/$PKG/components/ + defs/"
$CLI add synthetic_data_generator --auto-install
$CLI add datetime_parser          --auto-install
$CLI add rfm_segmentation         --auto-install
$CLI add cohort_analysis          --auto-install
$CLI add running_total            --auto-install
$CLI add dataframe_to_csv         --auto-install

# Three sinks → three target_dirs
$CLI add dataframe_to_csv --auto-install --target-dir "src/$PKG/defs/dataframe_to_csv_cohorts"
$CLI add dataframe_to_csv --auto-install --target-dir "src/$PKG/defs/dataframe_to_csv_spend"
$CLI add cron_schedule         --auto-install

echo ">>> Writing demo defs.yaml for each component"

cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: orders_raw
  schema_type: orders
  row_count: 2000
  random_state: 42
  description: 2000 synthetic e-commerce orders across many customers
  include_preview_metadata: true
  preview_rows: 25
  group_name: ingest
EOF

cat > "src/$PKG/defs/datetime_parser/defs.yaml" <<EOF
type: $PKG.components.datetime_parser.component.DatetimeParser
attributes:
  asset_name: orders_typed
  upstream_asset_key: orders_raw
  date_column: order_date
  input_format: "%Y-%m-%d %H:%M:%S"
  output_format: "%Y-%m-%d"
  include_preview_metadata: true
  group_name: transform
EOF

cat > "src/$PKG/defs/rfm_segmentation/defs.yaml" <<EOF
type: $PKG.components.rfm_segmentation.component.RFMSegmentationComponent
attributes:
  asset_name: customer_segments
  source_asset: orders_typed
  scoring_method: quintile
  lookback_days: 365
  customer_id_field: customer_id
  order_date_field: order_date
  order_id_field: order_id
  revenue_field: total
  include_preview_metadata: true
  group_name: analytics
EOF

cat > "src/$PKG/defs/cohort_analysis/defs.yaml" <<EOF
type: $PKG.components.cohort_analysis.component.CohortAnalysisComponent
attributes:
  asset_name: monthly_cohorts
  upstream_asset_key: orders_typed
  cohort_period: monthly
  retention_periods: 6
  customer_id_field: customer_id
  activity_date_field: order_date
  include_preview_metadata: true
  group_name: analytics
EOF

cat > "src/$PKG/defs/running_total/defs.yaml" <<EOF
type: $PKG.components.running_total.component.RunningTotalComponent
attributes:
  asset_name: customer_running_spend
  upstream_asset_key: orders_typed
  value_column: total
  output_column: cumulative_spend
  sort_by: order_date
  sort_ascending: true
  agg_function: sum
  group_by:
    - customer_id
  include_preview_metadata: true
  group_name: analytics
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: segments_report
  upstream_asset_key: customer_segments
  file_path: /tmp/retail_segments.csv
  include_index: false
  group_name: sink
EOF

cat > "src/$PKG/defs/dataframe_to_csv_cohorts/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: cohorts_report
  upstream_asset_key: monthly_cohorts
  file_path: /tmp/retail_cohorts.csv
  include_index: false
  group_name: sink
EOF

cat > "src/$PKG/defs/dataframe_to_csv_spend/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: spend_report
  upstream_asset_key: customer_running_spend
  file_path: /tmp/retail_running_spend.csv
  include_index: false
  group_name: sink
EOF

cat > "src/$PKG/defs/cron_schedule/defs.yaml" <<EOF
type: $PKG.components.cron_schedule.component.CronScheduleComponent
attributes:
  schedule_name: nightly_retail_refresh
  cron_expression: "0 4 * * *"
  asset_keys:
    - segments_report
    - cohorts_report
    - spend_report
  default_status: STOPPED
  tags:
    purpose: retail_refresh
EOF

cat <<MSG

>>> Setup complete.

Materialize headlessly:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Or open the UI:
    cd $PROJECT_DIR && uv run dg dev

Outputs (3 reports from the same upstream):
  /tmp/retail_segments.csv       — RFM-scored customer segments
  /tmp/retail_cohorts.csv        — monthly cohort retention matrix
  /tmp/retail_running_spend.csv  — cumulative spend per customer over time

Inspect:
    head -5 /tmp/retail_segments.csv
    head /tmp/retail_cohorts.csv
    head -5 /tmp/retail_running_spend.csv
MSG
