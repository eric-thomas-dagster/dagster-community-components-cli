#!/usr/bin/env bash
# Airline passengers forecast demo — canonical create-dagster + dg.
#
# The classic 1949-1960 monthly airline-passengers time series, fit with an
# Exponential Smoothing (Holt-Winters) model, forecast 24 months out, both
# the historical series and the forecast appended into one CSV.
#
# Pipeline (4 components, all autoloaded by `dg`):
#     csv_file_ingestion → datetime_parser → ets_forecast → dataframe_to_csv

set -euo pipefail

PROJECT_DIR="${1:-passengers-forecast-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas requests statsmodels
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 4 community components into src/$PKG/defs/"
$CLI add csv_file_ingestion    --auto-install
$CLI add datetime_parser       --auto-install
$CLI add ets_forecast          --auto-install
$CLI add dataframe_to_csv      --auto-install

echo ">>> Writing demo defs.yaml for each component"

# 1. Ingest — the canonical Box-Jenkins airline-passengers series
cat > "src/$PKG/defs/csv_file_ingestion/defs.yaml" <<EOF
type: $PKG.defs.csv_file_ingestion.component.CSVFileIngestionComponent
attributes:
  asset_name: passengers_raw
  file_path: https://raw.githubusercontent.com/jbrownlee/Datasets/master/airline-passengers.csv
  description: Monthly international airline passengers, 1949-1960
  group_name: ingest
EOF

# 2. Parse "1949-01" → first-of-month datetime
cat > "src/$PKG/defs/datetime_parser/defs.yaml" <<EOF
type: $PKG.defs.datetime_parser.component.DatetimeParser
attributes:
  asset_name: passengers_typed
  upstream_asset_key: passengers_raw
  date_column: Month
  input_format: "%Y-%m"
  group_name: transform
EOF

# 3. Fit ETS (Holt-Winters), forecast 24 months out, append to history
cat > "src/$PKG/defs/ets_forecast/defs.yaml" <<EOF
type: $PKG.defs.ets_forecast.component.EtsForecastComponent
attributes:
  asset_name: passengers_with_forecast
  upstream_asset_key: passengers_typed
  date_column: Month
  value_column: Passengers
  forecast_periods: 24
  trend: add
  seasonal: mul
  seasonal_periods: 12
  output_mode: append
  group_name: model
EOF

# 4. Write the combined history+forecast CSV
cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.defs.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: passengers_report
  upstream_asset_key: passengers_with_forecast
  file_path: /tmp/passengers_forecast.csv
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

Output: /tmp/passengers_forecast.csv — 144 historical months (Jan 1949 to
Dec 1960) plus 24 forecasted months (Jan 1961 to Dec 1962).

Inspect:
    head -3 /tmp/passengers_forecast.csv          # first historical rows
    tail -25 /tmp/passengers_forecast.csv         # forecasted tail

You'll see the model picks up the strong upward trend + 12-month seasonal
cycle and projects two more years of growth.
MSG
