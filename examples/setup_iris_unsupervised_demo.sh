#!/usr/bin/env bash
# Iris Unsupervised demo — PCA + K-Means on the canonical Iris dataset.
#
# Replaces the separate iris_clusters and iris_pca demos with one richer
# pipeline that runs both algorithms in sequence:
#   - Standard-scale the 4 numeric features
#   - PCA-project to 2D for visualization
#   - K-Means cluster on the SCALED features (not the PCA projection — better
#     when the original features are well-conditioned)
#   - Output a single DataFrame with both PC coordinates + cluster assignment

set -euo pipefail
PROJECT_DIR="${1:-iris-unsupervised-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding deps"
uv add -q pandas scikit-learn requests
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 5 community components"
$CLI add file_ingestion    --auto-install
$CLI add feature_scaler        --auto-install
$CLI add pca                   --auto-install
$CLI add k_means_clustering    --auto-install
$CLI add dataframe_to_csv      --auto-install

cat > "src/$PKG/defs/file_ingestion/defs.yaml" <<EOF
type: $PKG.components.file_ingestion.component.FileIngestionComponent
attributes:
  asset_name: iris_raw
  file_path: https://raw.githubusercontent.com/mwaskom/seaborn-data/master/iris.csv
  description: UCI Iris (3 species × 50 flowers, 4 numeric features)
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

cat > "src/$PKG/defs/pca/defs.yaml" <<EOF
type: $PKG.components.pca.component.PcaComponent
attributes:
  asset_name: iris_pca
  upstream_asset_key: iris_scaled
  feature_columns: [sepal_length, sepal_width, petal_length, petal_width]
  n_components: 2
  keep_original: true   # so kmeans downstream can still use the 4 original features
  group_name: model
EOF

cat > "src/$PKG/defs/k_means_clustering/defs.yaml" <<EOF
type: $PKG.components.k_means_clustering.component.KMeansClusteringComponent
attributes:
  asset_name: iris_clustered
  upstream_asset_key: iris_pca
  feature_columns: [sepal_length, sepal_width, petal_length, petal_width]
  n_clusters: 3
  output_column: cluster
  normalize: false   # already scaled
  random_state: 42
  include_distance: true
  group_name: model
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: iris_report
  upstream_asset_key: iris_clustered
  file_path: /tmp/iris_unsupervised.csv
  include_index: false
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.
Materialize:
    cd $PROJECT_DIR && uv run dg launch --assets '*'

Output: /tmp/iris_unsupervised.csv — every flower with PC1, PC2, cluster, and distance to centroid.

Inspect (kmeans should land roughly along species lines — Iris is famously well-separated):
    uv run python -c "
    import pandas as pd
    df = pd.read_csv('/tmp/iris_unsupervised.csv')
    print(pd.crosstab(df.species, df.cluster, margins=True))
    "
MSG
