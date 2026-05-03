#!/usr/bin/env bash
# Wine ML Pipeline demo — full classifier workflow with cross-validation.
#
# Builds a more substantial ML pipeline than the existing wine_demo:
#   - feature scaling (standardize the 11 chemistry features)
#   - train/test split via create_samples
#   - decision_tree_model with two parallel outputs (predictions + feature_importance)
#   - cross_validation to verify model stability across folds
#   - three CSV sinks
#
# Pipeline (8 components, all autoloaded by `dg`):
#                          ┌─→ create_samples ─┐
#                          │                    ├─→ decision_tree (predictions)        → CSV
#     csv_file_ingestion ─→ feature_scaler ──┐ │
#                          │                  ├┴→ decision_tree (feature_importance)   → CSV
#                          └────────────────→ cross_validation                          → CSV

set -euo pipefail

PROJECT_DIR="${1:-wine-ml-pipeline-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas scikit-learn requests
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 8 community components into src/$PKG/components/ + defs/"
$CLI add csv_file_ingestion    --auto-install
$CLI add feature_scaler        --auto-install
$CLI add create_samples        --auto-install
$CLI add decision_tree_model   --auto-install
$CLI add cross_validation      --auto-install
$CLI add dataframe_to_csv      --auto-install

# Second decision_tree instance (importance branch) + 2 more sink instances
$CLI add decision_tree_model --auto-install --target-dir "src/$PKG/defs/decision_tree_importance"
$CLI add dataframe_to_csv    --auto-install --target-dir "src/$PKG/defs/dataframe_to_csv_importance"
$CLI add dataframe_to_csv    --auto-install --target-dir "src/$PKG/defs/dataframe_to_csv_cv"
$CLI add cron_schedule         --auto-install

echo ">>> Writing demo defs.yaml for each component"

cat > "src/$PKG/defs/csv_file_ingestion/defs.yaml" <<EOF
type: $PKG.components.csv_file_ingestion.component.CSVFileIngestionComponent
attributes:
  asset_name: wine_raw
  file_path: https://archive.ics.uci.edu/ml/machine-learning-databases/wine-quality/winequality-red.csv
  description: UCI red wine quality dataset (1599 rows, 11 chemistry features, quality 3-8)
  delimiter: ";"
  include_preview_metadata: true
  preview_rows: 25
  group_name: ingest
EOF

cat > "src/$PKG/defs/feature_scaler/defs.yaml" <<EOF
type: $PKG.components.feature_scaler.component.FeatureScalerComponent
attributes:
  asset_name: wine_scaled
  upstream_asset_key: wine_raw
  strategy: standard
  columns:
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
  include_preview_metadata: true
  group_name: transform
EOF

cat > "src/$PKG/defs/create_samples/defs.yaml" <<EOF
type: $PKG.components.create_samples.component.CreateSamplesComponent
attributes:
  asset_name: wine_split
  upstream_asset_key: wine_scaled
  test_size: 0.2
  validation_size: 0.0
  random_state: 42
  stratify_column: quality
  output_split_column: split
  include_preview_metadata: true
  group_name: transform
EOF

cat > "src/$PKG/defs/decision_tree_model/defs.yaml" <<EOF
type: $PKG.components.decision_tree_model.component.DecisionTreeModelComponent
attributes:
  asset_name: wine_predictions
  upstream_asset_key: wine_split
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
  task_type: classification
  max_depth: 6
  test_size: 0.2
  random_state: 42
  output_mode: predictions
  include_preview_metadata: true
  group_name: model
EOF

cat > "src/$PKG/defs/decision_tree_importance/defs.yaml" <<EOF
type: $PKG.components.decision_tree_model.component.DecisionTreeModelComponent
attributes:
  asset_name: wine_feature_importance
  upstream_asset_key: wine_split
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
  task_type: classification
  max_depth: 6
  test_size: 0.2
  random_state: 42
  output_mode: feature_importance
  include_preview_metadata: true
  group_name: model
EOF

cat > "src/$PKG/defs/cross_validation/defs.yaml" <<EOF
type: $PKG.components.cross_validation.component.CrossValidationComponent
attributes:
  asset_name: wine_cv_scores
  upstream_asset_key: wine_scaled
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
  model_type: decision_tree
  task_type: classification
  cv_folds: 5
  random_state: 42
  include_preview_metadata: true
  group_name: validation
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: predictions_report
  upstream_asset_key: wine_predictions
  file_path: /tmp/wine_predictions.csv
  include_index: false
  group_name: sink
EOF

cat > "src/$PKG/defs/dataframe_to_csv_importance/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: importance_report
  upstream_asset_key: wine_feature_importance
  file_path: /tmp/wine_importance.csv
  include_index: false
  group_name: sink
EOF

cat > "src/$PKG/defs/dataframe_to_csv_cv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: cv_report
  upstream_asset_key: wine_cv_scores
  file_path: /tmp/wine_cv.csv
  include_index: false
  group_name: sink
EOF

cat > "src/$PKG/defs/cron_schedule/defs.yaml" <<EOF
type: $PKG.components.cron_schedule.component.CronScheduleComponent
attributes:
  schedule_name: weekly_wine_retrain
  cron_expression: "0 5 * * 1"
  asset_keys:
    - predictions_report
    - importance_report
    - cv_report
  default_status: STOPPED
  tags:
    purpose: wine_retrain
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Or open the UI:
    cd $PROJECT_DIR && uv run dg dev

Outputs:
  /tmp/wine_predictions.csv  — every wine + predicted quality + true quality
  /tmp/wine_importance.csv   — features ranked by tree importance
  /tmp/wine_cv.csv           — 5-fold cross-validation scores

Inspect:
    head -3 /tmp/wine_predictions.csv
    cat /tmp/wine_importance.csv
    cat /tmp/wine_cv.csv
MSG
