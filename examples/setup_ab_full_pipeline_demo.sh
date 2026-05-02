#!/usr/bin/env bash
# A/B Full Pipeline demo — assignment + analysis + monitoring + planning.
#
# An end-to-end experimentation pipeline:
#   1. eligible_users (synthetic) → ab_treatments (deterministic split)
#   2. exposure_events (synthetic) → ab_test_analysis  (significance verdict)
#                                  → ab_trend          (daily conv-rate trend)
#                                  → ab_controls       (sizing for next experiment)
#
# Pipeline (10 components, all autoloaded by `dg`):

set -euo pipefail
PROJECT_DIR="${1:-ab-full-pipeline-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding deps"
uv add -q pandas scipy
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 7 community components"
$CLI add synthetic_data_generator --auto-install
$CLI add ab_treatments            --auto-install
$CLI add ab_test_analysis         --auto-install
$CLI add ab_trend                 --auto-install
$CLI add ab_controls              --auto-install
$CLI add dataframe_to_csv         --auto-install
# Two synthetic instances (users + experiment) and 4 sinks
$CLI add synthetic_data_generator --auto-install --target-dir "src/$PKG/defs/exposure_gen"
$CLI add dataframe_to_csv         --auto-install --target-dir "src/$PKG/defs/dataframe_to_csv_assignments"
$CLI add dataframe_to_csv         --auto-install --target-dir "src/$PKG/defs/dataframe_to_csv_trend"
$CLI add dataframe_to_csv         --auto-install --target-dir "src/$PKG/defs/dataframe_to_csv_sizing"

echo ">>> Writing demo defs.yaml"

cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: eligible_users
  schema_type: users
  row_count: 5000
  random_state: 42
  group_name: ingest
EOF

cat > "src/$PKG/defs/exposure_gen/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: exposure_events
  schema_type: ab_experiment
  row_count: 5000
  random_state: 42
  schema_options:
    experiment_id: "checkout_button_v2"
    control_conversion: 0.10
    lift: 0.30
    treatment_share: 0.5
    lookback_days: 14
  group_name: ingest
EOF

cat > "src/$PKG/defs/ab_treatments/defs.yaml" <<EOF
type: $PKG.components.ab_treatments.component.ABTreatmentsComponent
attributes:
  asset_name: user_assignments
  upstream_asset_key: eligible_users
  user_id_column: user_id
  experiment_id: checkout_button_v2
  variants: [control, treatment]
  weights: [0.5, 0.5]
  output_column: variant
  group_name: experiment
EOF

cat > "src/$PKG/defs/ab_test_analysis/defs.yaml" <<EOF
type: $PKG.components.ab_test_analysis.component.ABTestAnalysisComponent
attributes:
  asset_name: ab_test_results
  upstream_asset_key: exposure_events
  experiment_id_field: experiment_id
  variant_field: variant
  user_id_field: user_id
  converted_field: converted
  timestamp_field: exposed_at
  confidence_level: 0.95
  minimum_sample_size: 100
  minimum_detectable_effect: 0.05
  group_name: analysis
EOF

cat > "src/$PKG/defs/ab_trend/defs.yaml" <<EOF
type: $PKG.components.ab_trend.component.ABTrendComponent
attributes:
  asset_name: daily_trend
  upstream_asset_key: exposure_events
  variant_column: variant
  converted_column: converted
  date_column: exposed_at
  bucket: day
  group_name: analysis
EOF

cat > "src/$PKG/defs/ab_controls/defs.yaml" <<EOF
type: $PKG.components.ab_controls.component.ABControlsComponent
attributes:
  asset_name: next_experiment_sizing
  upstream_asset_key: exposure_events
  baseline_rate: 0.13
  mde: 0.10
  alpha: 0.05
  power: 0.8
  group_name: planning
EOF

# Sinks
cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: results_report
  upstream_asset_key: ab_test_results
  file_path: /tmp/ab_results.csv
  include_index: false
EOF

cat > "src/$PKG/defs/dataframe_to_csv_assignments/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: assignments_report
  upstream_asset_key: user_assignments
  file_path: /tmp/ab_assignments.csv
  include_index: false
EOF

cat > "src/$PKG/defs/dataframe_to_csv_trend/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: trend_report
  upstream_asset_key: daily_trend
  file_path: /tmp/ab_trend.csv
  include_index: false
EOF

cat > "src/$PKG/defs/dataframe_to_csv_sizing/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: sizing_report
  upstream_asset_key: next_experiment_sizing
  file_path: /tmp/ab_sizing.csv
  include_index: false
EOF

cat <<MSG

>>> Setup complete.
Materialize:
    cd $PROJECT_DIR && uv run dg launch --assets '*'

Outputs:
  /tmp/ab_assignments.csv  — 5000 users randomly split into control/treatment
  /tmp/ab_results.csv      — significance verdict
  /tmp/ab_trend.csv        — daily conversion rates per variant
  /tmp/ab_sizing.csv       — required sample size for the NEXT experiment
MSG
