#!/usr/bin/env bash
# Open-Meteo weather demo — canonical create-dagster + dg.
#
# Hits the public Open-Meteo API (no auth, no key), pulls 14 days of NYC
# weather, parses dates, computes running precipitation, transposes into
# a "metric per day" matrix, writes a CSV.
#
# Pipeline (5 components, all autoloaded by `dg`):
#     rest_api_fetcher → datetime_parser → running_total → transpose → dataframe_to_csv

set -euo pipefail

PROJECT_DIR="${1:-weather-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas requests
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 5 community components into src/$PKG/components/ + defs/"
$CLI add rest_api_fetcher    --auto-install
$CLI add datetime_parser     --auto-install
$CLI add running_total       --auto-install
$CLI add transpose           --auto-install
$CLI add dataframe_to_csv    --auto-install

echo ">>> Writing demo defs.yaml for each component"

cat > "src/$PKG/defs/rest_api_fetcher/defs.yaml" <<EOF
type: $PKG.components.rest_api_fetcher.component.RestApiFetcherComponent
attributes:
  asset_name: weather_raw
  api_url: "https://api.open-meteo.com/v1/forecast?latitude=40.71&longitude=-74.01&daily=temperature_2m_max,temperature_2m_min,precipitation_sum&timezone=UTC&past_days=14&forecast_days=1"
  method: GET
  auth_type: none
  output_format: dataframe
  json_path: daily
  description: NYC daily weather, past 14 days + today
  group_name: ingest
EOF

cat > "src/$PKG/defs/datetime_parser/defs.yaml" <<EOF
type: $PKG.components.datetime_parser.component.DatetimeParser
attributes:
  asset_name: weather_typed
  upstream_asset_key: weather_raw
  date_column: time
  input_format: "%Y-%m-%d"
  output_format: "%Y-%m-%d"
  group_name: transform
EOF

cat > "src/$PKG/defs/running_total/defs.yaml" <<EOF
type: $PKG.components.running_total.component.RunningTotalComponent
attributes:
  asset_name: weather_with_cumulative_precip
  upstream_asset_key: weather_typed
  value_column: precipitation_sum
  output_column: cumulative_precip_mm
  sort_by: time
  sort_ascending: true
  agg_function: sum
  group_name: transform
EOF

cat > "src/$PKG/defs/transpose/defs.yaml" <<EOF
type: $PKG.components.transpose.component.TransposeComponent
attributes:
  asset_name: weather_by_date
  upstream_asset_key: weather_with_cumulative_precip
  index_column: time
  reset_column_name: metric
  group_name: transform
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: weather_report
  upstream_asset_key: weather_by_date
  file_path: /tmp/weather_report.csv
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

Output: /tmp/weather_report.csv — NYC weather pivoted, one row per metric,
one column per day.
MSG
