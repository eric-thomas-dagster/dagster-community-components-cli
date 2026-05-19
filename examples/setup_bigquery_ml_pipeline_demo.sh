#!/usr/bin/env bash
# BQ-native ML pipeline demo — CTAS + BQML train + BQML predict end-to-end.
#
# WHAT THIS DEMONSTRATES
#   A complete ML pipeline running entirely inside BigQuery — no Python
#   ML stack, no scikit-learn, no PyTorch. Trains and predicts via SQL.
#
# Asset graph:
#   iris_clean         ← bigquery_create_table_from_query_asset
#                        (CTAS from public iris dataset, drops nulls)
#         │
#         └── iris_logreg_model  ← bigquery_ml_train_asset
#                                  (LOGISTIC_REG on species)
#                  │
#                  └── iris_predictions  ← bigquery_ml_predict_asset
#                                          (ML.PREDICT on first 10 rows)
#                            │
#                            └── iris_predictions_csv  ← dataframe_to_csv
#
# REQUIRED ENV VAR
#   GOOGLE_APPLICATION_CREDENTIALS  service-account JSON
#
# COST while running
#   ~\$0.001. Iris is 150 rows, model training scans <1 KB.

set -euo pipefail
PROJECT_DIR="${1:-bigquery-ml-pipeline-demo}"

if [ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]; then
  echo "ERROR: set GOOGLE_APPLICATION_CREDENTIALS"
  exit 1
fi

DATASET="${BQ_DATASET:-servicepulse-490502.dagster_demo}"
PROJECT_ID="${DATASET%%.*}"
DS_NAME="${DATASET##*.}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas google-auth google-cloud-bigquery db-dtypes
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing BQ ML pipeline components"
$CLI add bigquery_create_table_from_query_asset --auto-install
$CLI add bigquery_ml_train_asset                --auto-install
$CLI add bigquery_ml_predict_asset              --auto-install
$CLI add dataframe_to_csv                       --auto-install

# 1) CTAS — clean staging table from the public iris dataset
mkdir -p "src/$PKG/defs/bigquery_create_table_from_query_asset"
cat > "src/$PKG/defs/bigquery_create_table_from_query_asset/defs.yaml" <<EOF
type: $PKG.components.bigquery_create_table_from_query_asset.component.BigQueryCreateTableFromQueryAssetComponent
attributes:
  asset_name: iris_clean
  credentials_path: "$GOOGLE_APPLICATION_CREDENTIALS"
  destination_table_id: $DATASET.iris_clean
  materialization: table
  query: |
    SELECT *
    FROM \`bigquery-public-data.ml_datasets.iris\`
    WHERE species IS NOT NULL
  table_options:
    description: "Cleaned iris training data for the BQML demo"
    labels: { tier: silver, owner: ml }
  description: Clean iris staging table — drops null species rows.
  group_name: warehouse
EOF

# 2) BQML train — logistic regression on species
mkdir -p "src/$PKG/defs/bigquery_ml_train_asset"
cat > "src/$PKG/defs/bigquery_ml_train_asset/defs.yaml" <<EOF
type: $PKG.components.bigquery_ml_train_asset.component.BigQueryMLTrainAssetComponent
attributes:
  asset_name: iris_logreg_model
  credentials_path: "$GOOGLE_APPLICATION_CREDENTIALS"
  destination_model_id: $DATASET.iris_logreg
  model_options:
    model_type: LOGISTIC_REG
    input_label_cols: [species]
    auto_class_weights: true
    data_split_method: AUTO_SPLIT
  select_query: |
    SELECT * FROM \`$DATASET.iris_clean\`
  deps: [iris_clean]
  description: BQML logistic regression predicting iris species.
  group_name: ml
EOF

# 3) BQML predict — score the first 10 rows
mkdir -p "src/$PKG/defs/bigquery_ml_predict_asset"
cat > "src/$PKG/defs/bigquery_ml_predict_asset/defs.yaml" <<EOF
type: $PKG.components.bigquery_ml_predict_asset.component.BigQueryMLPredictAssetComponent
attributes:
  asset_name: iris_predictions
  credentials_path: "$GOOGLE_APPLICATION_CREDENTIALS"
  model_id: $DATASET.iris_logreg
  operation: predict
  input_query: |
    SELECT * EXCEPT (species)
    FROM \`$DATASET.iris_clean\`
    LIMIT 10
  deps: [iris_logreg_model]
  description: Predicted iris species + probabilities for the first 10 rows.
  group_name: ml
EOF

# 4) CSV sink for inspection
mkdir -p "src/$PKG/defs/dataframe_to_csv"
cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: iris_predictions_csv
  upstream_asset_key: iris_predictions
  file_path: /tmp/iris_predictions.csv
  include_index: false
  description: CSV export of the iris predictions.
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Asset graph:
    iris_clean              ← BQ CTAS  (clean staging from public iris)
          │
          └── iris_logreg_model     ← BQML train (LOGISTIC_REG)
                  │
                  └── iris_predictions   ← BQML predict (ML.PREDICT)
                            │
                            └── iris_predictions_csv  ← /tmp/iris_predictions.csv

Materialize all four:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Inspect:
    cat /tmp/iris_predictions.csv
    bq show $DATASET.iris_clean
    bq show -m $DATASET.iris_logreg

Cost: ~\$0.001 (iris is 150 rows, training scans <1 KB).
MSG
