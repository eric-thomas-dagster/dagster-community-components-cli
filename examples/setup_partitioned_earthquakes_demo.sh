#!/usr/bin/env bash
# Partitioned USGS Earthquakes demo — backfillable across any date range.
# Canonical create-dagster + dg layout, daily-partitioned.
#
# Each daily partition queries the USGS historical API for that one day and
# writes a partition-suffixed JSONL file.
#
# Pipeline (5 components, all autoloaded by `dg`, daily-partitioned):
#     rest_api_fetcher → json_flatten → select_columns → sort → dataframe_to_json

set -euo pipefail

PROJECT_DIR="${1:-partitioned-earthquakes-demo}"
PARTITION_START="${2:-2026-04-01}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR (partitions start $PARTITION_START)"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas requests
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 5 community components into src/$PKG/components/ + defs/"
$CLI add rest_api_fetcher    --auto-install
$CLI add json_flatten        --auto-install
$CLI add select_columns      --auto-install
$CLI add sort                --auto-install
$CLI add dataframe_to_json   --auto-install

echo ">>> Writing demo defs.yaml for each component (daily-partitioned)"

cat > "src/$PKG/defs/rest_api_fetcher/defs.yaml" <<EOF
type: $PKG.components.rest_api_fetcher.component.RestApiFetcherComponent
attributes:
  asset_name: earthquakes_raw
  api_url: https://earthquake.usgs.gov/fdsnws/event/1/query
  method: GET
  params: '{"format": "geojson", "starttime": "{partition_date}", "endtime": "{partition_date_next}", "minmagnitude": 4}'
  auth_type: none
  output_format: dataframe
  json_path: features
  partition_type: daily
  partition_start: "$PARTITION_START"
  description: USGS earthquake feed — daily-partitioned, M>=4, public, no auth
  group_name: ingest
EOF

cat > "src/$PKG/defs/json_flatten/defs.yaml" <<EOF
type: $PKG.components.json_flatten.component.JsonFlattenComponent
attributes:
  asset_name: earthquakes_flat
  upstream_asset_key: earthquakes_raw
  column: properties
  separator: _
  max_depth: 2
  drop_original: true
  partition_type: daily
  partition_start: "$PARTITION_START"
  group_name: transform
EOF

cat > "src/$PKG/defs/select_columns/defs.yaml" <<EOF
type: $PKG.components.select_columns.component.SelectColumnsComponent
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
  partition_type: daily
  partition_start: "$PARTITION_START"
  group_name: transform
EOF

cat > "src/$PKG/defs/sort/defs.yaml" <<EOF
type: $PKG.components.sort.component.SortComponent
attributes:
  asset_name: earthquakes_sorted
  upstream_asset_key: earthquakes_clean
  by: [magnitude]
  ascending: false
  na_position: last
  reset_index: true
  partition_type: daily
  partition_start: "$PARTITION_START"
  group_name: transform
EOF

cat > "src/$PKG/defs/dataframe_to_json/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_json.component.DataframeToJsonComponent
attributes:
  asset_name: earthquakes_report
  upstream_asset_key: earthquakes_sorted
  file_path: /tmp/earthquakes_{partition_key}.jsonl
  orient: records
  lines: true
  date_format: iso
  partition_type: daily
  partition_start: "$PARTITION_START"
  group_name: sink
EOF

cat <<MSG

>>> Setup complete. Partitioned daily, start=$PARTITION_START.

Materialize a single day:
    cd $PROJECT_DIR
    uv run dg launch --assets '*' --partition 2026-04-15

Materialize a range (backfill):
    uv run dg launch --assets '*' --partition-range 2026-04-10...2026-04-15

Or open the UI and pick partitions there:
    uv run dg dev

Output: /tmp/earthquakes_<partition_date>.jsonl per day, biggest magnitude first.
MSG
