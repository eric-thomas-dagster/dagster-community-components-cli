#!/usr/bin/env bash
# Wine Quality ML demo — canonical create-dagster + dg.
#
# Pulls the UCI red-wine quality dataset, trains a random-forest regressor,
# emits both per-row predictions and a feature-importance ranking — two
# parallel branches off the same source asset.
#
# Pipeline (4 components, all autoloaded by `dg`):
#
#                        ┌─→ random_forest_model (predictions)        → CSV
#     csv_file_ingestion ┤
#                        └─→ random_forest_model (feature_importance) → CSV

set -euo pipefail

PROJECT_DIR="${1:-wine-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas scikit-learn requests
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 3 community components into src/$PKG/defs/"
$CLI add csv_file_ingestion    --auto-install
$CLI add random_forest_model   --auto-install
$CLI add dataframe_to_csv      --auto-install

# Install the model + sink twice — once per branch — into separate dirs
# so each gets its own defs.yaml. Re-running `add` into an existing dir
# is fine because the marker says "we put this here".
$CLI add random_forest_model   --auto-install --target-dir "src/$PKG/defs/random_forest_importance"
$CLI add dataframe_to_csv      --auto-install --target-dir "src/$PKG/defs/dataframe_to_csv_importance"

echo ">>> Writing demo defs.yaml for each component"

# 1. Ingest — UCI red wine, semicolon-separated
cat > "src/$PKG/defs/csv_file_ingestion/defs.yaml" <<EOF
type: $PKG.components.csv_file_ingestion.component.CSVFileIngestionComponent
attributes:
  asset_name: wine_raw
  file_path: https://archive.ics.uci.edu/ml/machine-learning-databases/wine-quality/winequality-red.csv
  description: UCI red wine quality dataset — 1599 rows, 11 features, quality score 3-8
  delimiter: ";"
  group_name: ingest
EOF

# 2a. Train + predict — adds `predicted` column to the full dataframe
cat > "src/$PKG/defs/random_forest_model/defs.yaml" <<EOF
type: $PKG.components.random_forest_model.component.RandomForestModelComponent
attributes:
  asset_name: wine_predictions
  upstream_asset_key: wine_raw
  target_column: quality
  feature_columns:
    - "fixed acidity"
    - "volatile acidity"
    - "citric acid"
    - "residual sugar"
    - "chlorides"
    - "free sulfur dioxide"
    - "total sulfur dioxide"
    - "density"
    - "pH"
    - "sulphates"
    - "alcohol"
  task_type: regression
  n_estimators: 200
  max_depth: 10
  test_size: 0.2
  random_state: 42
  output_mode: predictions
  group_name: model
EOF

# 2b. Same training, different output mode — feature importance ranking
cat > "src/$PKG/defs/random_forest_importance/defs.yaml" <<EOF
type: $PKG.components.random_forest_importance.component.RandomForestModelComponent
attributes:
  asset_name: wine_feature_importance
  upstream_asset_key: wine_raw
  target_column: quality
  feature_columns:
    - "fixed acidity"
    - "volatile acidity"
    - "citric acid"
    - "residual sugar"
    - "chlorides"
    - "free sulfur dioxide"
    - "total sulfur dioxide"
    - "density"
    - "pH"
    - "sulphates"
    - "alcohol"
  task_type: regression
  n_estimators: 200
  max_depth: 10
  test_size: 0.2
  random_state: 42
  output_mode: feature_importance
  group_name: model
EOF

# 3a. Sink — predictions to CSV
cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: wine_predictions_report
  upstream_asset_key: wine_predictions
  file_path: /tmp/wine_predictions.csv
  include_index: false
  group_name: sink
EOF

# 3b. Sink — feature importance to CSV
cat > "src/$PKG/defs/dataframe_to_csv_importance/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv_importance.component.DataframeToCsvComponent
attributes:
  asset_name: wine_feature_importance_report
  upstream_asset_key: wine_feature_importance
  file_path: /tmp/wine_feature_importance.csv
  include_index: false
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Materialize headlessly:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Or open the UI:
    cd $PROJECT_DIR && uv run dg dev   # http://localhost:3000

Outputs:
  /tmp/wine_predictions.csv          — every wine + its model-predicted quality
  /tmp/wine_feature_importance.csv   — features ranked by importance

Inspect:
    head -3 /tmp/wine_predictions.csv
    cat /tmp/wine_feature_importance.csv
MSG
