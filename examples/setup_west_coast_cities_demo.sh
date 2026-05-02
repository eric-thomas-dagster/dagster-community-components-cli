#!/usr/bin/env bash
# West-coast cities demo — geographic bounding-box filter.
#
# Same 10-city CSV from the cities_distance demo, but this time the
# bounding_box_filter component keeps only cities west of lng -100 and
# south of lat 38 (loosely the US west coast / Sun Belt). The output
# is a CSV of just those cities.
#
# Pipeline (3 components, all autoloaded by `dg`):
#     csv_file_ingestion → bounding_box_filter → dataframe_to_csv

set -euo pipefail

PROJECT_DIR="${1:-west-coast-cities-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas
uv add --dev -q dagster-dg-cli dagster-webserver

echo ">>> Generating the 10-city CSV"
cat > /tmp/us_cities.csv <<'EOF'
city,lat,lng
New York,40.7128,-74.0060
Los Angeles,34.0522,-118.2437
Chicago,41.8781,-87.6298
Houston,29.7604,-95.3698
Phoenix,33.4484,-112.0740
Philadelphia,39.9526,-75.1652
San Antonio,29.4241,-98.4936
San Diego,32.7157,-117.1611
Dallas,32.7767,-96.7970
San Francisco,37.7749,-122.4194
EOF

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 3 community components into src/$PKG/components/ + defs/"
$CLI add csv_file_ingestion   --auto-install
$CLI add bounding_box_filter  --auto-install
$CLI add dataframe_to_csv     --auto-install

echo ">>> Writing demo defs.yaml for each component"

cat > "src/$PKG/defs/csv_file_ingestion/defs.yaml" <<EOF
type: $PKG.components.csv_file_ingestion.component.CSVFileIngestionComponent
attributes:
  asset_name: us_cities
  file_path: /tmp/us_cities.csv
  description: 10 major US cities with lat/lng
  group_name: ingest
EOF

cat > "src/$PKG/defs/bounding_box_filter/defs.yaml" <<EOF
type: $PKG.components.bounding_box_filter.component.BoundingBoxFilterComponent
attributes:
  asset_name: west_coast_cities
  upstream_asset_key: us_cities
  lat_column: lat
  lng_column: lng
  min_lat: 24.0
  max_lat: 38.0
  min_lng: -125.0
  max_lng: -100.0
  keep_outside: false
  include_preview_metadata: true
  preview_rows: 25
  group_name: filter
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: west_coast_report
  upstream_asset_key: west_coast_cities
  file_path: /tmp/west_coast_cities.csv
  include_index: false
  include_preview_metadata: true
  preview_rows: 25
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Materialize headlessly:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Or open the UI:
    cd $PROJECT_DIR && uv run dg dev

Output: /tmp/west_coast_cities.csv — only the cities inside the bounding box
(west of -100 lng, south of 38 lat).

Note: the bounding_box_filter and dataframe_to_csv assets each include a
'preview' MetadataValue.md in the catalog — a Dagster builder UI can
render this without warehouse access. Open dg dev → asset details →
Materialization metadata to see it.
MSG
