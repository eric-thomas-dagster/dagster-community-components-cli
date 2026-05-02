#!/usr/bin/env bash
# SpaceX launches × rockets join demo — canonical create-dagster + dg.
#
# Two REST sources merged into one report. Pulls launches (each with a
# `rocket` ID reference) and rockets (with the rocket name + specs),
# joins on rocket ID, writes the enriched table. First demo to use
# dataframe_join for a real multi-source pipeline.
#
# Pipeline (5 components, all autoloaded by `dg`):
#
#     rest_api_fetcher (launches) ┐
#                                  ├─→ dataframe_join → select_columns → CSV
#     rest_api_fetcher (rockets) ─┘

set -euo pipefail

PROJECT_DIR="${1:-spacex-join-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas requests
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing components into src/$PKG/defs/"
$CLI add rest_api_fetcher    --auto-install
$CLI add dataframe_join      --auto-install
$CLI add select_columns      --auto-install
$CLI add dataframe_to_csv    --auto-install

# Two REST fetches into separate dirs — same component used twice
$CLI add rest_api_fetcher    --auto-install --target-dir "src/$PKG/defs/rest_rockets"

echo ">>> Writing demo defs.yaml for each component"

# 1a. Fetch launches
cat > "src/$PKG/defs/rest_api_fetcher/defs.yaml" <<EOF
type: $PKG.components.rest_api_fetcher.component.RestApiFetcherComponent
attributes:
  asset_name: launches
  api_url: https://api.spacexdata.com/v4/launches
  method: GET
  auth_type: none
  output_format: dataframe
  description: All SpaceX launches
  group_name: ingest
EOF

# 1b. Fetch rockets (4 of them — Falcon 1, Falcon 9, Falcon Heavy, Starship)
cat > "src/$PKG/defs/rest_rockets/defs.yaml" <<EOF
type: $PKG.components.rest_rockets.component.RestApiFetcherComponent
attributes:
  asset_name: rockets
  api_url: https://api.spacexdata.com/v4/rockets
  method: GET
  auth_type: none
  output_format: dataframe
  description: SpaceX rocket catalog (Falcon 1 / 9 / Heavy / Starship)
  group_name: ingest
EOF

# 2. Join — launches.rocket = rockets.id
cat > "src/$PKG/defs/dataframe_join/defs.yaml" <<EOF
type: $PKG.components.dataframe_join.component.DataframeJoin
attributes:
  asset_name: launches_with_rocket
  left_asset_key: launches
  right_asset_key: rockets
  how: left
  left_on: [rocket]
  right_on: [id]
  suffixes: ["_launch", "_rocket"]
  group_name: transform
EOF

# 3. Select human-meaningful columns
cat > "src/$PKG/defs/select_columns/defs.yaml" <<EOF
type: $PKG.components.select_columns.component.SelectColumnsComponent
attributes:
  asset_name: launches_clean
  upstream_asset_key: launches_with_rocket
  columns: [name_launch, date_utc, success, flight_number, name_rocket, mass, height]
  rename:
    name_launch: launch_name
    name_rocket: rocket_name
  reorder: true
  group_name: transform
EOF

# 4. Write
cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: launches_report
  upstream_asset_key: launches_clean
  file_path: /tmp/spacex_with_rockets.csv
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

Output: /tmp/spacex_with_rockets.csv — every SpaceX launch enriched with
its rocket's name and specs (height, mass).

Inspect launches per rocket:
    awk -F, 'NR>1{print \$5}' /tmp/spacex_with_rockets.csv | sort | uniq -c | sort -rn
MSG
