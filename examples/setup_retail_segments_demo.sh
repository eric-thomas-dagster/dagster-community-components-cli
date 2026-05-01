#!/usr/bin/env bash
# Customer segmentation demo — RFM scoring on UCI Online Retail.
#
# Same source dataset as the LTV demo, but routed through customer_segmentation
# (lineage-based RFM analysis) which expects standard column names. Shows how
# select_columns + rename adapts a real-world schema to a registry component's
# expected contract.
#
# Pipeline (6 components, all autoloaded by `dg`):
#     csv_file_ingestion → data_cleansing → formula → select_columns
#                        → customer_segmentation → dataframe_to_csv

set -euo pipefail

PROJECT_DIR="${1:-retail-segments-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas requests
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 6 community components into src/$PKG/defs/"
$CLI add csv_file_ingestion       --auto-install
$CLI add data_cleansing           --auto-install
$CLI add formula                  --auto-install
$CLI add select_columns           --auto-install
$CLI add customer_segmentation    --auto-install
$CLI add dataframe_to_csv         --auto-install

echo ">>> Writing demo defs.yaml for each component"

cat > "src/$PKG/defs/csv_file_ingestion/defs.yaml" <<EOF
type: $PKG.defs.csv_file_ingestion.component.CSVFileIngestionComponent
attributes:
  asset_name: retail_raw
  file_path: https://raw.githubusercontent.com/databricks/Spark-The-Definitive-Guide/master/data/retail-data/all/online-retail-dataset.csv
  description: UCI Online Retail dataset
  group_name: ingest
EOF

cat > "src/$PKG/defs/data_cleansing/defs.yaml" <<EOF
type: $PKG.defs.data_cleansing.component.DataCleansingComponent
attributes:
  asset_name: retail_clean
  upstream_asset_key: retail_raw
  null_handling: drop
  columns: [CustomerID]
  group_name: transform
EOF

cat > "src/$PKG/defs/formula/defs.yaml" <<EOF
type: $PKG.defs.formula.component.FormulaComponent
attributes:
  asset_name: retail_with_amount
  upstream_asset_key: retail_clean
  expressions:
    amount: "Quantity * UnitPrice"
  group_name: transform
EOF

# Map UCI column names → the canonical names customer_segmentation expects
cat > "src/$PKG/defs/select_columns/defs.yaml" <<EOF
type: $PKG.defs.select_columns.component.SelectColumnsComponent
attributes:
  asset_name: transactions
  upstream_asset_key: retail_with_amount
  columns: [CustomerID, InvoiceDate, amount]
  rename:
    CustomerID: customer_id
    InvoiceDate: date
  reorder: true
  group_name: transform
EOF

cat > "src/$PKG/defs/customer_segmentation/defs.yaml" <<EOF
type: $PKG.defs.customer_segmentation.component.CustomerSegmentationComponent
attributes:
  asset_name: customer_segments
  transaction_data_asset: transactions
  scoring_method: quintiles
  recency_weight: 1.0
  frequency_weight: 1.0
  monetary_weight: 1.0
  # The UCI dataset is from 2010-2011, so a default 365-day window would
  # filter out everything. 6000 days = "all of it".
  analysis_period_days: 6000
  use_predefined_segments: true
  group_name: model
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.defs.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: customer_segments_report
  upstream_asset_key: customer_segments
  file_path: /tmp/customer_segments.csv
  include_index: false
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Output: /tmp/customer_segments.csv — one row per customer with R/F/M
scores (1-5), the weighted RFM score, and a named segment (Champions,
Loyal Customers, At Risk, Lost, etc.).

Inspect — segment distribution:
    uv run python -c "
    import pandas as pd
    df = pd.read_csv('/tmp/customer_segments.csv')
    print(f'Customers: {len(df)}')
    print(df.segment.value_counts().to_string())
    "
MSG
