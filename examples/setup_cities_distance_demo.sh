#!/usr/bin/env bash
# Geo distance demo — canonical create-dagster + dg.
#
# Generates a small CSV of 10 major US cities with lat/lng, cross-joins it
# to itself to produce all 100 city pairs, computes haversine distance for
# each pair, drops self-pairs, sorts by distance, writes a CSV. Real
# pairwise distance matrix from a 6-component pipeline.
#
# Pipeline (6 components, all autoloaded by `dg`):
#     csv_file_ingestion ─┐
#                          ├─→ dataframe_join (cross)
#     csv_file_ingestion ─┘   → distance_calculator → filter
#                                                   → sort
#                                                   → dataframe_to_csv

set -euo pipefail

PROJECT_DIR="${1:-cities-distance-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas
uv add --dev -q dagster-dg-cli dagster-webserver

echo ">>> Generating a 10-city CSV with lat/lng"
cat > /tmp/cities.csv <<'EOF'
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

echo ">>> Installing components into src/$PKG/defs/"
$CLI add csv_file_ingestion       --auto-install
$CLI add dataframe_join           --auto-install
$CLI add distance_calculator      --auto-install
$CLI add filter                   --auto-install
$CLI add sort                     --auto-install
$CLI add dataframe_to_csv         --auto-install
# Second ingest for the right-side of the cross join
$CLI add csv_file_ingestion       --auto-install --target-dir "src/$PKG/defs/csv_destinations"

echo ">>> Writing demo defs.yaml for each component"

# 1a + 1b. Both ingests read the same file but produce two distinct assets
# so the cross join has two inputs to fan into.
cat > "src/$PKG/defs/csv_file_ingestion/defs.yaml" <<EOF
type: $PKG.defs.csv_file_ingestion.component.CSVFileIngestionComponent
attributes:
  asset_name: cities_origin
  file_path: /tmp/cities.csv
  description: Origin cities (left side of pair)
  group_name: ingest
EOF

cat > "src/$PKG/defs/csv_destinations/defs.yaml" <<EOF
type: $PKG.defs.csv_destinations.component.CSVFileIngestionComponent
attributes:
  asset_name: cities_dest
  file_path: /tmp/cities.csv
  description: Destination cities (right side of pair)
  group_name: ingest
EOF

# 2. Cross-join — every origin paired with every destination (100 rows)
cat > "src/$PKG/defs/dataframe_join/defs.yaml" <<EOF
type: $PKG.defs.dataframe_join.component.DataframeJoin
attributes:
  asset_name: city_pairs
  left_asset_key: cities_origin
  right_asset_key: cities_dest
  how: cross
  suffixes: [_origin, _dest]
  group_name: transform
EOF

# 3. Compute haversine distance per pair
cat > "src/$PKG/defs/distance_calculator/defs.yaml" <<EOF
type: $PKG.defs.distance_calculator.component.DistanceCalculatorComponent
attributes:
  asset_name: pairs_with_distance
  upstream_asset_key: city_pairs
  lat1_column: lat_origin
  lng1_column: lng_origin
  lat2_column: lat_dest
  lng2_column: lng_dest
  output_column: distance_km
  unit: km
  formula: haversine
  group_name: transform
EOF

# 4. Drop self-pairs (city == city)
cat > "src/$PKG/defs/filter/defs.yaml" <<EOF
type: $PKG.defs.filter.component.FilterComponent
attributes:
  asset_name: pairs_unique
  upstream_asset_key: pairs_with_distance
  condition: "city_origin != city_dest"
  group_name: transform
EOF

# 5. Sort by distance
cat > "src/$PKG/defs/sort/defs.yaml" <<EOF
type: $PKG.defs.sort.component.SortComponent
attributes:
  asset_name: pairs_sorted
  upstream_asset_key: pairs_unique
  by: [distance_km]
  ascending: true
  group_name: transform
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.defs.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: pairs_report
  upstream_asset_key: pairs_sorted
  file_path: /tmp/city_distances.csv
  include_index: false
  columns: [city_origin, city_dest, distance_km]
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Output: /tmp/city_distances.csv — every (origin, destination, km)
triple, sorted shortest-first. 10×10 cross-join minus 10 self-pairs = 90 rows.

Inspect — closest + farthest pairs:
    head -5 /tmp/city_distances.csv
    echo "---"
    tail -5 /tmp/city_distances.csv
MSG
