#!/usr/bin/env bash
# Titanic Complete demo — full data-science workflow on one dataset.
#
# A larger companion to the focused titanic_demo / titanic_etl_demo /
# titanic_logreg_demo / titanic_quality_demo demos. This one walks the
# whole journey in a single pipeline:
#   ingest → quality (dedup+cleanse+outliers) → ETL (impute, type-coerce,
#   bin, one-hot) → model (logistic regression) → outputs (predictions,
#   summary stats, survivors-only).
#
# Pipeline (12 components, all autoloaded by `dg`):
#                                                                      ┌─→ logistic_regression  → CSV
#   csv_file_ingestion                                                  │
#     → unique_dedup → data_cleansing → outlier_clipper                 │
#     → imputation → type_coercer → tile_binning → one_hot_encoding ─┬─┘
#                                                                    │
#                                                                    ├─→ summarize (EDA)        → CSV
#                                                                    │
#                                                                    └─→ filter (survivors)     → CSV

set -euo pipefail

PROJECT_DIR="${1:-titanic-complete-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas scikit-learn
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 12 community components into src/$PKG/components/ + defs/"
$CLI add csv_file_ingestion        --auto-install
$CLI add unique_dedup              --auto-install
$CLI add data_cleansing            --auto-install
$CLI add outlier_clipper           --auto-install
$CLI add imputation                --auto-install
$CLI add type_coercer              --auto-install
$CLI add tile_binning              --auto-install
$CLI add one_hot_encoding          --auto-install
$CLI add logistic_regression_model --auto-install
$CLI add summarize                 --auto-install
$CLI add filter                    --auto-install
$CLI add dataframe_to_csv          --auto-install

# 3 sink instances (predictions, eda, survivors)
$CLI add dataframe_to_csv --auto-install --target-dir "src/$PKG/defs/dataframe_to_csv_eda"
$CLI add dataframe_to_csv --auto-install --target-dir "src/$PKG/defs/dataframe_to_csv_survivors"
$CLI add cron_schedule         --auto-install

echo ">>> Writing demo defs.yaml for each component"

# --- 1. Ingest ---
cat > "src/$PKG/defs/csv_file_ingestion/defs.yaml" <<EOF
type: $PKG.components.csv_file_ingestion.component.CSVFileIngestionComponent
attributes:
  asset_name: titanic_raw
  file_path: https://raw.githubusercontent.com/datasciencedojo/datasets/master/titanic.csv
  description: Titanic survival dataset (891 passengers, mix of demographics + survival flag)
  include_preview_metadata: true
  group_name: ingest
EOF

# --- 2. Quality ---
cat > "src/$PKG/defs/unique_dedup/defs.yaml" <<EOF
type: $PKG.components.unique_dedup.component.UniqueDedupComponent
attributes:
  asset_name: titanic_dedup
  upstream_asset_key: titanic_raw
  subset:
    - PassengerId
  keep: first
  include_preview_metadata: true
  group_name: quality
EOF

cat > "src/$PKG/defs/data_cleansing/defs.yaml" <<EOF
type: $PKG.components.data_cleansing.component.DataCleansingComponent
attributes:
  asset_name: titanic_cleansed
  upstream_asset_key: titanic_dedup
  trim_whitespace: true
  normalize_case: lower
  columns:
    - Name
    - Sex
    - Embarked
    - Ticket
  include_preview_metadata: true
  group_name: quality
EOF

cat > "src/$PKG/defs/outlier_clipper/defs.yaml" <<EOF
type: $PKG.components.outlier_clipper.component.OutlierClipperComponent
attributes:
  asset_name: titanic_clipped
  upstream_asset_key: titanic_cleansed
  columns:
    - Fare
    - Age
  strategy: iqr
  iqr_multiplier: 1.5
  include_preview_metadata: true
  group_name: quality
EOF

# --- 3. ETL ---
cat > "src/$PKG/defs/imputation/defs.yaml" <<EOF
type: $PKG.components.imputation.component.ImputationComponent
attributes:
  asset_name: titanic_imputed
  upstream_asset_key: titanic_clipped
  columns:
    - Age
    - Fare
  strategy: median
  include_preview_metadata: true
  group_name: etl
EOF

cat > "src/$PKG/defs/type_coercer/defs.yaml" <<EOF
type: $PKG.components.type_coercer.component.TypeCoercerComponent
attributes:
  asset_name: titanic_typed
  upstream_asset_key: titanic_imputed
  type_map:
    Pclass: int
    Survived: int
    Age: float
    Fare: float
  errors: coerce
  include_preview_metadata: true
  group_name: etl
EOF

cat > "src/$PKG/defs/tile_binning/defs.yaml" <<EOF
type: $PKG.components.tile_binning.component.TileBinningComponent
attributes:
  asset_name: titanic_binned
  upstream_asset_key: titanic_typed
  column: Age
  output_column: age_band
  n_bins: 4
  labels:
    - child
    - young_adult
    - adult
    - senior
  include_preview_metadata: true
  group_name: etl
EOF

cat > "src/$PKG/defs/one_hot_encoding/defs.yaml" <<EOF
type: $PKG.components.one_hot_encoding.component.OneHotEncodingComponent
attributes:
  asset_name: titanic_encoded
  upstream_asset_key: titanic_binned
  columns:
    - Sex
    - Embarked
  drop_first: true
  include_preview_metadata: true
  group_name: etl
EOF

# --- 4. Model ---
cat > "src/$PKG/defs/logistic_regression_model/defs.yaml" <<EOF
type: $PKG.components.logistic_regression_model.component.LogisticRegressionModelComponent
attributes:
  asset_name: titanic_predictions
  upstream_asset_key: titanic_encoded
  target_column: Survived
  feature_columns:
    - Pclass
    - Age
    - SibSp
    - Parch
    - Fare
    - Sex_male
    - Embarked_q
    - Embarked_s
  test_size: 0.2
  random_state: 42
  include_preview_metadata: true
  group_name: model
EOF

# --- 5. EDA branches off the typed dataset ---
cat > "src/$PKG/defs/summarize/defs.yaml" <<EOF
type: $PKG.components.summarize.component.SummarizeComponent
attributes:
  asset_name: titanic_eda
  upstream_asset_key: titanic_typed
  group_by:
    - Pclass
  aggregations:
    Age: mean
    Fare: mean
    Survived: mean
    PassengerId: count
  include_preview_metadata: true
  group_name: eda
EOF

cat > "src/$PKG/defs/filter/defs.yaml" <<EOF
type: $PKG.components.filter.component.FilterComponent
attributes:
  asset_name: titanic_survivors
  upstream_asset_key: titanic_typed
  condition: "Survived == 1"
  include_preview_metadata: true
  group_name: eda
EOF

# --- Sinks ---
cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: predictions_report
  upstream_asset_key: titanic_predictions
  file_path: /tmp/titanic_predictions.csv
  include_index: false
  group_name: sink
EOF

cat > "src/$PKG/defs/dataframe_to_csv_eda/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: eda_report
  upstream_asset_key: titanic_eda
  file_path: /tmp/titanic_eda.csv
  include_index: false
  group_name: sink
EOF

cat > "src/$PKG/defs/dataframe_to_csv_survivors/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: survivors_report
  upstream_asset_key: titanic_survivors
  file_path: /tmp/titanic_survivors.csv
  include_index: false
  group_name: sink
EOF

cat > "src/$PKG/defs/cron_schedule/defs.yaml" <<EOF
type: $PKG.components.cron_schedule.component.CronScheduleComponent
attributes:
  schedule_name: nightly_titanic_refresh
  cron_expression: "0 3 * * *"
  asset_keys:
    - predictions_report
    - eda_report
    - survivors_report
  default_status: STOPPED
  tags:
    purpose: titanic_refresh
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Or open the UI:
    cd $PROJECT_DIR && uv run dg dev

Outputs:
  /tmp/titanic_predictions.csv  — survival predictions from logistic regression
  /tmp/titanic_eda.csv          — summary stats by passenger class
  /tmp/titanic_survivors.csv    — only the rows where Survived=1

Companion to the focused titanic / titanic_etl / titanic_logreg /
titanic_quality demos — this one shows them all combined in a single
end-to-end pipeline.
MSG
