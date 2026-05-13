#!/usr/bin/env bash
# Store Coverage demo — geospatial pipeline with 9 components.
#
# Builds service-area zones around 5 retail stores, finds which of 100
# customers fall inside each zone, computes per-store coverage stats,
# and tiles the area into a 50km grid for heatmap-style aggregation.
#
# Pipeline (9 components, all autoloaded by `dg`):
#   csv_file_ingestion (stores)    → create_points → buffer → smooth
#                                                              │
#   csv_file_ingestion (customers) → create_points              ├─→ spatial_join → summarize → CSV
#                                                              │
#                                          → make_grid (heatmap tiles)              → CSV

set -euo pipefail

PROJECT_DIR="${1:-store-coverage-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps (geopandas pulls shapely + pyproj)"
uv add -q pandas geopandas shapely numpy
uv add --dev -q dagster-dg-cli dagster-webserver

echo ">>> Generating the 5-store + 100-customer CSVs"
cat > /tmp/stores.csv <<'EOF'
store_id,name,lat,lng
S001,Manhattan,40.7589,-73.9851
S002,Brooklyn,40.6782,-73.9442
S003,Queens,40.7282,-73.7949
S004,Bronx,40.8448,-73.8648
S005,Jersey City,40.7178,-74.0431
EOF

# Note: customers are generated 100%-components via parametric_data_generator
# below — no inline Python, no /tmp/customers.csv. The stores still come from
# the inline CSV above since they're a small fixed set.

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 9 community components"
$CLI add csv_file_ingestion        --auto-install
$CLI add parametric_data_generator --auto-install
$CLI add create_points             --auto-install
$CLI add buffer                    --auto-install
$CLI add smooth                    --auto-install
$CLI add make_grid                 --auto-install
$CLI add spatial_join              --auto-install
$CLI add summarize                 --auto-install
$CLI add dataframe_to_csv          --auto-install

# Dual ingest + dual create_points + dual sinks via target_dir
mkdir -p "src/$PKG/defs/customers_ingest"  # only needs defs.yaml; component code is in components/csv_file_ingestion/
mkdir -p "src/$PKG/defs/customers_points"  # only needs defs.yaml; component code is in components/create_points/
mkdir -p "src/$PKG/defs/dataframe_to_csv_grid"  # only needs defs.yaml; component code is in components/dataframe_to_csv/
$CLI add cron_schedule         --auto-install

echo ">>> Writing demo defs.yaml for each component"

# --- Stores branch ---
cat > "src/$PKG/defs/csv_file_ingestion/defs.yaml" <<EOF
type: $PKG.components.csv_file_ingestion.component.CSVFileIngestionComponent
attributes:
  asset_name: stores_raw
  file_path: /tmp/stores.csv
  description: 5 NYC-area retail stores
  group_name: ingest
EOF

cat > "src/$PKG/defs/create_points/defs.yaml" <<EOF
type: $PKG.components.create_points.component.CreatePointsComponent
attributes:
  asset_name: stores_with_geom
  upstream_asset_key: stores_raw
  lat_column: lat
  lng_column: lng
  group_name: spatial
EOF

cat > "src/$PKG/defs/buffer/defs.yaml" <<EOF
type: $PKG.components.buffer.component.BufferComponent
attributes:
  asset_name: store_service_areas
  upstream_asset_key: stores_with_geom
  geometry_column: geometry
  radius_meters: 5000
  group_name: spatial
EOF

cat > "src/$PKG/defs/smooth/defs.yaml" <<EOF
type: $PKG.components.smooth.component.SmoothComponent
attributes:
  asset_name: store_service_areas_smooth
  upstream_asset_key: store_service_areas
  geometry_column: geometry
  tolerance_meters: 200
  group_name: spatial
EOF

# --- Customers branch (100% components — synthetic via parametric_data_generator) ---
cat > "src/$PKG/defs/customers_ingest/defs.yaml" <<EOF
type: $PKG.components.parametric_data_generator.component.ParametricDataGeneratorComponent
attributes:
  asset_name: customers_raw
  row_count: 100
  random_state: 42
  description: 100 synthetic NYC-area customers with lat/lng + lifetime value
  group_name: ingest
  columns:
    customer_id:
      type: id
      prefix: "C"
      width: 4
    lat:
      type: float
      min: 40.55
      max: 40.90
      precision: 4
    lng:
      type: float
      min: -74.20
      max: -73.70
      precision: 4
    lifetime_value:
      type: float
      min: 100
      max: 5000
      precision: 2
EOF

cat > "src/$PKG/defs/customers_points/defs.yaml" <<EOF
type: $PKG.components.create_points.component.CreatePointsComponent
attributes:
  asset_name: customers_with_geom
  upstream_asset_key: customers_raw
  lat_column: lat
  lng_column: lng
  group_name: spatial
EOF

# --- Spatial join + aggregation ---
cat > "src/$PKG/defs/spatial_join/defs.yaml" <<EOF
type: $PKG.components.spatial_join.component.SpatialJoinComponent
attributes:
  asset_name: customers_assigned_to_stores
  upstream_asset_key: customers_raw
  regions_asset_key: store_service_areas_smooth
  lat_column: lat
  lng_column: lng
  geometry_column: geometry
  how: inner
  group_name: spatial
EOF

cat > "src/$PKG/defs/summarize/defs.yaml" <<EOF
type: $PKG.components.summarize.component.SummarizeComponent
attributes:
  asset_name: coverage_per_store
  upstream_asset_key: customers_assigned_to_stores
  group_by:
    - name
  aggregations:
    customer_id: count
    lifetime_value: sum
  group_name: analytics
EOF

# --- Heatmap grid (parallel branch) ---
cat > "src/$PKG/defs/make_grid/defs.yaml" <<EOF
type: $PKG.components.make_grid.component.MakeGridComponent
attributes:
  asset_name: nyc_grid
  upstream_asset_key: customers_with_geom
  cell_size_meters: 5000
  group_name: spatial
EOF

# --- Sinks ---
cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: coverage_report
  upstream_asset_key: coverage_per_store
  file_path: /tmp/store_coverage.csv
  include_index: false
  group_name: sink
EOF

cat > "src/$PKG/defs/dataframe_to_csv_grid/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: grid_report
  upstream_asset_key: nyc_grid
  file_path: /tmp/nyc_grid_cells.csv
  include_index: false
  group_name: sink
EOF

cat > "src/$PKG/defs/cron_schedule/defs.yaml" <<EOF
type: $PKG.components.cron_schedule.component.CronScheduleComponent
attributes:
  schedule_name: weekly_coverage_refresh
  cron_expression: "0 7 * * 1"
  asset_keys:
    - coverage_report
    - grid_report
  default_status: STOPPED
  tags:
    purpose: coverage_refresh
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Or open the UI:
    cd $PROJECT_DIR && uv run dg dev

Outputs:
  /tmp/store_coverage.csv  — customer count + total lifetime value per store
  /tmp/nyc_grid_cells.csv  — 5km grid cells over NYC metro

Inspect:
    cat /tmp/store_coverage.csv
    head /tmp/nyc_grid_cells.csv
MSG
