#!/usr/bin/env bash
# Titanic logistic regression demo — canonical create-dagster + dg.
#
# Predicts passenger survival (binary classification) from a small set of
# features. Demonstrates a real classifier (vs. the wine demo's regressor)
# and shows how `imputation` + `one_hot_encoding` chain into a model in YAML.
#
# Pipeline (5 components, all autoloaded by `dg`):
#     csv_file_ingestion → imputation → one_hot_encoding
#                        → logistic_regression_model → dataframe_to_csv

set -euo pipefail

PROJECT_DIR="${1:-titanic-logreg-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas scikit-learn requests
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 5 community components into src/$PKG/components/ + defs/"
$CLI add csv_file_ingestion         --auto-install
$CLI add imputation                 --auto-install
$CLI add one_hot_encoding           --auto-install
$CLI add logistic_regression_model  --auto-install
$CLI add dataframe_to_csv           --auto-install

echo ">>> Writing demo defs.yaml for each component"

cat > "src/$PKG/defs/csv_file_ingestion/defs.yaml" <<EOF
type: $PKG.components.csv_file_ingestion.component.CSVFileIngestionComponent
attributes:
  asset_name: titanic_raw
  file_path: https://raw.githubusercontent.com/datasciencedojo/datasets/master/titanic.csv
  description: Titanic — 891 passengers, target = Survived (0/1)
  group_name: ingest
EOF

# Fill missing Age (only numeric feature with NaNs in Titanic)
cat > "src/$PKG/defs/imputation/defs.yaml" <<EOF
type: $PKG.components.imputation.component.ImputationComponent
attributes:
  asset_name: titanic_imputed
  upstream_asset_key: titanic_raw
  strategy: median
  columns: [Age, Fare]
  group_name: transform
EOF

# Encode Sex into Sex_male / Sex_female columns the model can consume
cat > "src/$PKG/defs/one_hot_encoding/defs.yaml" <<EOF
type: $PKG.components.one_hot_encoding.component.OneHotEncodingComponent
attributes:
  asset_name: titanic_encoded
  upstream_asset_key: titanic_imputed
  columns: [Sex]
  drop_first: false
  group_name: transform
EOF

# Fit a binary classifier on a small interpretable feature set
cat > "src/$PKG/defs/logistic_regression_model/defs.yaml" <<EOF
type: $PKG.components.logistic_regression_model.component.LogisticRegressionModelComponent
attributes:
  asset_name: titanic_predictions
  upstream_asset_key: titanic_encoded
  target_column: Survived
  feature_columns: [Pclass, Age, SibSp, Parch, Fare, Sex_male, Sex_female]
  test_size: 0.2
  random_state: 42
  max_iter: 1000
  output_predictions: true
  output_probabilities: true
  normalize: true
  group_name: model
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: titanic_predictions_report
  upstream_asset_key: titanic_predictions
  file_path: /tmp/titanic_predictions.csv
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

Output: /tmp/titanic_predictions.csv — every passenger plus
predicted_class (0/1) and predicted_proba_0 / predicted_proba_1.

Inspect — accuracy + actual vs predicted crosstab:
    uv run python -c "
    import pandas as pd
    df = pd.read_csv('/tmp/titanic_predictions.csv')
    print('accuracy:', (df.Survived == df.predicted_class).mean().round(3))
    print(pd.crosstab(df.Survived, df.predicted_class, margins=True))
    "
MSG
