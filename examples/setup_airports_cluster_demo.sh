#!/usr/bin/env bash
# Geo-spatial clustering demo — canonical create-dagster + dg.
#
# Pulls 3,376 airports (vega-datasets airports.csv, US-only with lat/lng),
# runs DBSCAN spatial clustering with a 50km neighborhood radius, writes
# each airport's cluster ID. DBSCAN finds dense regions (major metros)
# and labels everything else as noise (-1).
#
# Pipeline (3 components, all autoloaded by `dg`):
#     csv_file_ingestion → spatial_cluster → dataframe_to_csv

set -euo pipefail

PROJECT_DIR="${1:-airports-cluster-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas scikit-learn requests
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 3 community components into src/$PKG/components/ + defs/"
$CLI add csv_file_ingestion    --auto-install
$CLI add spatial_cluster       --auto-install
$CLI add dataframe_to_csv      --auto-install

echo ">>> Writing demo defs.yaml for each component"

cat > "src/$PKG/defs/csv_file_ingestion/defs.yaml" <<EOF
type: $PKG.components.csv_file_ingestion.component.CSVFileIngestionComponent
attributes:
  asset_name: airports_raw
  file_path: https://raw.githubusercontent.com/vega/vega-datasets/main/data/airports.csv
  description: Vega airports.csv — ~3.4k US airports with lat/lng
  group_name: ingest
EOF

cat > "src/$PKG/defs/spatial_cluster/defs.yaml" <<EOF
type: $PKG.components.spatial_cluster.component.SpatialClusterComponent
attributes:
  asset_name: airports_clustered
  upstream_asset_key: airports_raw
  lat_column: latitude
  lng_column: longitude
  algorithm: dbscan
  eps_km: 50.0
  min_samples: 5
  output_column: cluster_id
  group_name: model
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: airports_report
  upstream_asset_key: airports_clustered
  file_path: /tmp/airports_clusters.csv
  include_index: false
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Output: /tmp/airports_clusters.csv — every airport tagged with a
DBSCAN cluster_id (-1 = noise/outlier, 0+ = cluster ID).

Inspect — top 10 metro clusters by airport count:
    uv run python -c "
    import pandas as pd
    df = pd.read_csv('/tmp/airports_clusters.csv')
    print(f'Total airports: {len(df)}')
    print(f'Noise (rural/isolated): {(df.cluster_id == -1).sum()}')
    print(f'Distinct metro clusters: {df[df.cluster_id >= 0].cluster_id.nunique()}')
    top = df[df.cluster_id >= 0].groupby('cluster_id').size().nlargest(10)
    for cid, count in top.items():
        sample = df[df.cluster_id == cid][['city','state']].head(3).values.tolist()
        print(f'  cluster {cid}: {count} airports — e.g. {sample}')
    "
MSG
