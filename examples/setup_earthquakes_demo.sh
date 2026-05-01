#!/usr/bin/env bash
# USGS Earthquakes JSON pipeline demo — canonical create-dagster + dg.
#
# Hits the USGS public earthquake feed (no auth), flattens nested JSON,
# selects + renames columns, sorts by magnitude, writes JSONL output.
#
# Pipeline (5 components, all autoloaded by `dg`):
#     rest_api_fetcher → json_flatten → select_columns → sort → dataframe_to_json

set -euo pipefail

PROJECT_DIR="${1:-earthquakes-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas requests
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 5 community components into src/$PKG/defs/"
$CLI add rest_api_fetcher    --auto-install
$CLI add json_flatten        --auto-install
$CLI add select_columns      --auto-install
$CLI add sort                --auto-install
$CLI add dataframe_to_json   --auto-install

echo ">>> Writing demo defs.yaml for each component"

cat > "src/$PKG/defs/rest_api_fetcher/defs.yaml" <<EOF
type: $PKG.defs.rest_api_fetcher.component.RestApiFetcherComponent
attributes:
  asset_name: earthquakes_raw
  api_url: https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson
  method: GET
  auth_type: none
  output_format: dataframe
  json_path: features
  description: USGS earthquake feed — last 24 hours, public, no auth
  group_name: ingest
EOF

cat > "src/$PKG/defs/json_flatten/defs.yaml" <<EOF
type: $PKG.defs.json_flatten.component.JsonFlattenComponent
attributes:
  asset_name: earthquakes_flat
  upstream_asset_key: earthquakes_raw
  column: properties
  separator: _
  max_depth: 2
  drop_original: true
  group_name: transform
EOF

cat > "src/$PKG/defs/select_columns/defs.yaml" <<EOF
type: $PKG.defs.select_columns.component.SelectColumnsComponent
attributes:
  asset_name: earthquakes_clean
  upstream_asset_key: earthquakes_flat
  columns: [id, properties_mag, properties_place, properties_time, properties_url]
  rename:
    properties_mag: magnitude
    properties_place: place
    properties_time: timestamp_ms
    properties_url: usgs_url
  reorder: true
  group_name: transform
EOF

cat > "src/$PKG/defs/sort/defs.yaml" <<EOF
type: $PKG.defs.sort.component.SortComponent
attributes:
  asset_name: earthquakes_sorted
  upstream_asset_key: earthquakes_clean
  by: [magnitude]
  ascending: false
  na_position: last
  reset_index: true
  group_name: transform
EOF

cat > "src/$PKG/defs/dataframe_to_json/defs.yaml" <<EOF
type: $PKG.defs.dataframe_to_json.component.DataframeToJsonComponent
attributes:
  asset_name: earthquakes_report
  upstream_asset_key: earthquakes_sorted
  file_path: /tmp/earthquakes.jsonl
  orient: records
  lines: true
  date_format: iso
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Materialize headlessly:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Or open the UI:
    cd $PROJECT_DIR && uv run dg dev   # http://localhost:3000

Output: /tmp/earthquakes.jsonl — one JSON line per earthquake, biggest
magnitude first, last 24 hours. Results vary day-to-day (live feed).
MSG
