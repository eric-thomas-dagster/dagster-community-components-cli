#!/usr/bin/env bash
# Iris clustering demo — canonical create-dagster + dg.
#
# Pulls the classic Iris dataset, scales the four numeric features, runs
# k-means with k=3, writes per-flower cluster assignments to CSV.
#
# Pipeline (4 components, all autoloaded by `dg`):
#     csv_file_ingestion → feature_scaler → k_means_clustering → dataframe_to_csv

set -euo pipefail

PROJECT_DIR="${1:-iris-clusters-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas scikit-learn requests
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 4 community components into src/$PKG/defs/"
$CLI add csv_file_ingestion    --auto-install
$CLI add feature_scaler        --auto-install
$CLI add k_means_clustering    --auto-install
$CLI add dataframe_to_csv      --auto-install

echo ">>> Writing demo defs.yaml for each component"

cat > "src/$PKG/defs/csv_file_ingestion/defs.yaml" <<EOF
type: $PKG.components.csv_file_ingestion.component.CSVFileIngestionComponent
attributes:
  asset_name: iris_raw
  file_path: https://raw.githubusercontent.com/mwaskom/seaborn-data/master/iris.csv
  description: UCI Iris dataset (3 species × 50 flowers, 4 numeric features)
  group_name: ingest
EOF

cat > "src/$PKG/defs/feature_scaler/defs.yaml" <<EOF
type: $PKG.components.feature_scaler.component.FeatureScalerComponent
attributes:
  asset_name: iris_scaled
  upstream_asset_key: iris_raw
  columns: [sepal_length, sepal_width, petal_length, petal_width]
  strategy: standard
  group_name: transform
EOF

cat > "src/$PKG/defs/k_means_clustering/defs.yaml" <<EOF
type: $PKG.components.k_means_clustering.component.KMeansClusteringComponent
attributes:
  asset_name: iris_clustered
  upstream_asset_key: iris_scaled
  feature_columns: [sepal_length, sepal_width, petal_length, petal_width]
  n_clusters: 3
  output_column: cluster
  normalize: false   # already scaled upstream
  random_state: 42
  include_distance: true
  group_name: model
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: iris_report
  upstream_asset_key: iris_clustered
  file_path: /tmp/iris_clusters.csv
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

Output: /tmp/iris_clusters.csv — 150 flowers, each tagged with a
cluster (0/1/2) and its distance to the nearest centroid.

Inspect the cluster vs species crosstab — k-means should land roughly
along species lines, since Iris is famously well-separated:

    uv run python -c "
    import pandas as pd
    df = pd.read_csv('/tmp/iris_clusters.csv')
    print(pd.crosstab(df.species, df.cluster, margins=True))
    "
MSG
