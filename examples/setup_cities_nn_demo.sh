#!/usr/bin/env bash
# Nearest-neighbors demo — for each city, find its top-3 nearest peers.
#
# Reuses the 10-city CSV from the distance demo, but instead of an
# all-pairs cross-join + filter, runs `nearest_neighbors` directly: each
# row gets its 3 closest-cities indices and distances added as columns.
#
# Pipeline (3 components, all autoloaded by `dg`):
#     file_ingestion → nearest_neighbors → dataframe_to_csv

set -euo pipefail

PROJECT_DIR="${1:-cities-nn-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas scikit-learn
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

echo ">>> Generating a 10-city CSV (same as the distance demo)"
cat > /tmp/cities_nn.csv <<'EOF'
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
$CLI add file_ingestion    --auto-install
$CLI add nearest_neighbors     --auto-install
$CLI add dataframe_to_csv      --auto-install

echo ">>> Writing demo defs.yaml for each component"

cat > "src/$PKG/defs/file_ingestion/defs.yaml" <<EOF
type: $PKG.components.file_ingestion.component.FileIngestionComponent
attributes:
  asset_name: cities
  file_path: /tmp/cities_nn.csv
  description: 10 major US cities with lat/lng
  group_name: ingest
EOF

cat > "src/$PKG/defs/nearest_neighbors/defs.yaml" <<EOF
type: $PKG.components.nearest_neighbors.component.NearestNeighborsComponent
attributes:
  asset_name: cities_with_neighbors
  upstream_asset_key: cities
  feature_columns: [lat, lng]
  n_neighbors: 4   # 4 because the closest is always self; we'll keep #1 as self-distance check
  metric: euclidean
  normalize: false   # raw lat/lng — close enough for ~regional distances
  output_distances: true
  output_indices: true
  group_name: model
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: cities_nn_report
  upstream_asset_key: cities_with_neighbors
  file_path: /tmp/cities_nearest.csv
  include_index: false
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Output: /tmp/cities_nearest.csv — every city + neighbor_N_idx and
neighbor_N_dist columns for N=1..4 (1 is the city itself, 2-4 are
its three closest peers).

Inspect — what's NYC's closest cluster?
    uv run python -c "
    import pandas as pd
    df = pd.read_csv('/tmp/cities_nearest.csv')
    nyc = df[df.city == 'New York'].iloc[0]
    print(f'NYC neighbors:')
    for i in range(2, 5):
        idx = int(nyc[f'neighbor_{i}_idx'])
        dist = nyc[f'neighbor_{i}_dist']
        print(f'  {df.iloc[idx].city} (lat/lng distance {dist:.2f})')
    "
MSG
