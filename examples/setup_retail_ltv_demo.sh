#!/usr/bin/env bash
# Customer Data Platform demo — UCI Online Retail → LTV prediction.
#
# Pulls the canonical UCI Online Retail dataset (542k transactions from a
# UK e-commerce site, 2010-2011), cleans missing CustomerIDs, computes
# per-line revenue, predicts each customer's 12-month lifetime value.
#
# Pipeline (5 components, all autoloaded by `dg`):
#     csv_file_ingestion → data_cleansing → formula
#                        → ltv_prediction → dataframe_to_csv

set -euo pipefail

PROJECT_DIR="${1:-retail-ltv-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas scikit-learn requests
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 5 community components into src/$PKG/defs/"
$CLI add csv_file_ingestion    --auto-install
$CLI add data_cleansing        --auto-install
$CLI add formula               --auto-install
$CLI add ltv_prediction        --auto-install
$CLI add dataframe_to_csv      --auto-install

echo ">>> Writing demo defs.yaml for each component"

# 1. Ingest — UCI Online Retail (UK e-commerce 2010-2011, ~540k transactions)
cat > "src/$PKG/defs/csv_file_ingestion/defs.yaml" <<EOF
type: $PKG.components.csv_file_ingestion.component.CSVFileIngestionComponent
attributes:
  asset_name: retail_raw
  file_path: https://raw.githubusercontent.com/databricks/Spark-The-Definitive-Guide/master/data/retail-data/all/online-retail-dataset.csv
  description: UCI Online Retail dataset — UK e-commerce, 2010-2011, ~540k transactions
  group_name: ingest
EOF

# 2. Cleanse — drop rows where CustomerID is missing (~25% of raw data has no CustomerID)
cat > "src/$PKG/defs/data_cleansing/defs.yaml" <<EOF
type: $PKG.components.data_cleansing.component.DataCleansingComponent
attributes:
  asset_name: retail_clean
  upstream_asset_key: retail_raw
  null_handling: drop
  columns: [CustomerID]
  group_name: transform
EOF

# 3. Compute per-line revenue: amount = Quantity * UnitPrice
cat > "src/$PKG/defs/formula/defs.yaml" <<EOF
type: $PKG.components.formula.component.FormulaComponent
attributes:
  asset_name: retail_with_amount
  upstream_asset_key: retail_clean
  expressions:
    amount: "Quantity * UnitPrice"
  group_name: transform
EOF

# 4. Predict per-customer LTV — auto-detects via specified column names
cat > "src/$PKG/defs/ltv_prediction/defs.yaml" <<EOF
type: $PKG.components.ltv_prediction.component.LTVPredictionComponent
attributes:
  asset_name: customer_ltv
  upstream_asset_key: retail_with_amount
  customer_id_field: CustomerID
  transaction_date_field: InvoiceDate
  amount_field: amount
  prediction_period_months: 12
  cohort_analysis: false
  include_confidence_intervals: false
  min_transactions_required: 2
  group_name: model
EOF

# 5. Sink
cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: customer_ltv_report
  upstream_asset_key: customer_ltv
  file_path: /tmp/customer_ltv.csv
  include_index: false
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Materialize headlessly (the ingest step downloads ~45MB on first run):
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Or open the UI:
    cd $PROJECT_DIR && uv run dg dev

Output: /tmp/customer_ltv.csv — one row per qualifying customer with
their predicted 12-month LTV plus historical aggregates.

Inspect — top 5 highest-LTV customers:
    uv run python -c "
    import pandas as pd
    df = pd.read_csv('/tmp/customer_ltv.csv')
    cols = ['customer_id','total_transactions','historical_ltv','predicted_total_ltv','value_segment']
    print(f'Customers scored: {len(df)}')
    print(df.sort_values('predicted_total_ltv', ascending=False).head(5)[cols].to_string(index=False))
    "
MSG
