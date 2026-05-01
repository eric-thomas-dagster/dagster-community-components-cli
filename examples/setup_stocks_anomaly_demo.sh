#!/usr/bin/env bash
# Stocks anomaly detection demo — canonical create-dagster + dg.
#
# Pulls vega's classic stocks dataset (5 tickers × 10 years monthly), runs
# anomaly detection grouped by ticker (so MSFT outliers are evaluated against
# MSFT history, not AMZN), writes a flagged report.
#
# Pipeline (3 components, all autoloaded by `dg`):
#     csv_file_ingestion → anomaly_detection → dataframe_to_csv

set -euo pipefail

PROJECT_DIR="${1:-stocks-anomaly-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas requests
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 3 community components into src/$PKG/defs/"
$CLI add csv_file_ingestion    --auto-install
$CLI add anomaly_detection     --auto-install
$CLI add dataframe_to_csv      --auto-install

echo ">>> Writing demo defs.yaml for each component"

cat > "src/$PKG/defs/csv_file_ingestion/defs.yaml" <<EOF
type: $PKG.defs.csv_file_ingestion.component.CSVFileIngestionComponent
attributes:
  asset_name: stocks_raw
  file_path: https://raw.githubusercontent.com/vega/vega-datasets/main/data/stocks.csv
  description: Vega stocks — MSFT/AMZN/IBM/GOOG/AAPL monthly close, 2000-2010
  group_name: ingest
EOF

cat > "src/$PKG/defs/anomaly_detection/defs.yaml" <<EOF
type: $PKG.defs.anomaly_detection.component.AnomalyDetectionComponent
attributes:
  asset_name: stocks_with_anomalies
  upstream_asset_key: stocks_raw
  metric_column: price
  detection_method: z_score
  threshold: 2.5
  group_by_field: symbol
  timestamp_field: date
  group_name: model
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.defs.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: stocks_anomaly_report
  upstream_asset_key: stocks_with_anomalies
  file_path: /tmp/stocks_anomalies.csv
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

Output: /tmp/stocks_anomalies.csv — every monthly close + an is_anomaly flag.
Z-score is computed within each symbol so MSFT's history is evaluated against
itself, not pooled with AMZN's much wider price range.

Inspect:
    awk -F, 'NR==1{print; next} \$NF=="True"' /tmp/stocks_anomalies.csv | head -10
MSG
