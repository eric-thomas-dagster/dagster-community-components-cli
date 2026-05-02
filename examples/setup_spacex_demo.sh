#!/usr/bin/env bash
# SpaceX Launches demo — canonical create-dagster + dg.
#
# Hits the public SpaceX API (no auth), pulls launch records, parses the
# UTC date strings into proper datetimes, ranks launches by date, writes
# an Excel report.
#
# Pipeline (5 components, all autoloaded by `dg`):
#     rest_api_fetcher → select_columns → datetime_parser → rank → dataframe_to_excel

set -euo pipefail

PROJECT_DIR="${1:-spacex-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas requests openpyxl
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 5 community components into src/$PKG/defs/"
$CLI add rest_api_fetcher    --auto-install
$CLI add select_columns      --auto-install
$CLI add datetime_parser     --auto-install
$CLI add rank                --auto-install
$CLI add dataframe_to_excel  --auto-install

echo ">>> Writing demo defs.yaml for each component"

cat > "src/$PKG/defs/rest_api_fetcher/defs.yaml" <<EOF
type: $PKG.components.rest_api_fetcher.component.RestApiFetcherComponent
attributes:
  asset_name: launches_raw
  api_url: https://api.spacexdata.com/v4/launches
  method: GET
  auth_type: none
  output_format: dataframe
  description: All SpaceX launches — public API, no auth
  group_name: ingest
EOF

cat > "src/$PKG/defs/select_columns/defs.yaml" <<EOF
type: $PKG.components.select_columns.component.SelectColumnsComponent
attributes:
  asset_name: launches_clean
  upstream_asset_key: launches_raw
  columns: [name, date_utc, success, rocket, flight_number, details]
  reorder: true
  group_name: transform
EOF

cat > "src/$PKG/defs/datetime_parser/defs.yaml" <<EOF
type: $PKG.components.datetime_parser.component.DatetimeParser
attributes:
  asset_name: launches_dated
  upstream_asset_key: launches_clean
  date_column: date_utc
  output_column: launch_date
  extract_components: false
  group_name: transform
EOF

cat > "src/$PKG/defs/rank/defs.yaml" <<EOF
type: $PKG.components.rank.component.RankComponent
attributes:
  asset_name: launches_ranked
  upstream_asset_key: launches_dated
  column: launch_date
  method: dense
  ascending: false
  output_column: rank_by_date
  group_name: transform
EOF

cat > "src/$PKG/defs/dataframe_to_excel/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_excel.component.DataframeToExcelComponent
attributes:
  asset_name: launches_report
  upstream_asset_key: launches_ranked
  file_path: /tmp/spacex_launches.xlsx
  sheet_name: Launches
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

Output: /tmp/spacex_launches.xlsx — every SpaceX launch ever, ranked
by date (newest=1), with parsed datetime + flight metadata.
MSG
