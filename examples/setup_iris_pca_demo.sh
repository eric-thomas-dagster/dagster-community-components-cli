#!/usr/bin/env bash
# Iris PCA demo — canonical create-dagster + dg.
#
# Same Iris dataset as the clustering demo, but reduced from 4 features
# (sepal/petal × length/width) down to 2 principal components for a 2D
# representation that explains ~95% of the variance.
#
# Pipeline (3 components, all autoloaded by `dg`):
#     csv_file_ingestion → pca → dataframe_to_csv

set -euo pipefail

PROJECT_DIR="${1:-iris-pca-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas scikit-learn requests
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 3 community components into src/$PKG/defs/"
$CLI add csv_file_ingestion    --auto-install
$CLI add pca                   --auto-install
$CLI add dataframe_to_csv      --auto-install

echo ">>> Writing demo defs.yaml for each component"

cat > "src/$PKG/defs/csv_file_ingestion/defs.yaml" <<EOF
type: $PKG.components.csv_file_ingestion.component.CSVFileIngestionComponent
attributes:
  asset_name: iris_raw
  file_path: https://raw.githubusercontent.com/mwaskom/seaborn-data/master/iris.csv
  description: UCI Iris dataset
  group_name: ingest
EOF

cat > "src/$PKG/defs/pca/defs.yaml" <<EOF
type: $PKG.components.pca.component.PcaComponent
attributes:
  asset_name: iris_pcs
  upstream_asset_key: iris_raw
  feature_columns: [sepal_length, sepal_width, petal_length, petal_width]
  n_components: 2
  output_prefix: "pc_"
  normalize: true
  keep_original: false
  include_explained_variance: true
  group_name: model
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: iris_pca_report
  upstream_asset_key: iris_pcs
  file_path: /tmp/iris_pca.csv
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

Output: /tmp/iris_pca.csv — 150 flowers, 4-D feature space collapsed to
2 principal components. The two PCs together explain ~95% of variance;
species cleanly separate along PC1.

Inspect:
    head -5 /tmp/iris_pca.csv
MSG
