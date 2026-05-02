#!/usr/bin/env bash
# A/B test stats demo — synthetic exposure data → significance + lift report.
#
# synthetic_data_generator (schema_type: ab_experiment) produces 5000
# control/treatment exposure rows where treatment converts 30% better.
# ab_test_analysis runs a stat test and reports lift, p-value, sample
# size, etc. Output is one row per variant.
#
# Pipeline (3 components, all autoloaded by `dg`):
#     synthetic_data_generator → ab_test_analysis → dataframe_to_csv

set -euo pipefail

PROJECT_DIR="${1:-ab-test-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas scipy
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 3 community components into src/$PKG/components/ + defs/"
$CLI add synthetic_data_generator --auto-install
$CLI add ab_test_analysis         --auto-install
$CLI add dataframe_to_csv         --auto-install

echo ">>> Writing demo defs.yaml for each component"

cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: experiment_data
  schema_type: ab_experiment
  row_count: 5000
  random_seed: 42
  schema_options:
    experiment_id: "checkout_button_v2"
    control_conversion: 0.10
    lift: 0.30                # treatment is 30% better (relative)
    treatment_share: 0.5
    lookback_days: 14
  description: Synthetic A/B exposure rows — checkout button v2 vs control
  group_name: ingest
EOF

cat > "src/$PKG/defs/ab_test_analysis/defs.yaml" <<EOF
type: $PKG.components.ab_test_analysis.component.ABTestAnalysisComponent
attributes:
  asset_name: ab_test_results
  upstream_asset_key: experiment_data
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

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: ab_test_report
  upstream_asset_key: ab_test_results
  file_path: /tmp/ab_test_results.csv
  include_index: false
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Materialize headlessly:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Or open the UI:
    cd $PROJECT_DIR && uv run dg dev

Output: /tmp/ab_test_results.csv — per-variant conversion rate, lift, and
significance verdict.

Inspect:
    cat /tmp/ab_test_results.csv
MSG
