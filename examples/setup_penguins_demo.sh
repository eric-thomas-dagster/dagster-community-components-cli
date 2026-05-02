#!/usr/bin/env bash
# Palmer Penguins ML feature engineering demo — canonical create-dagster + dg.
#
# Pulls Palmer Penguins data, fills missing values, one-hot encodes categorical
# columns, standard-scales numeric features, writes the ML-ready feature matrix
# to /tmp/penguins_features.parquet.
#
# Pipeline (5 components, all autoloaded by `dg`):
#     csv_file_ingestion → imputation → one_hot_encoding → feature_scaler → dataframe_to_parquet

set -euo pipefail

PROJECT_DIR="${1:-penguins-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas pyarrow scikit-learn requests
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 5 community components into src/$PKG/components/ + defs/"
$CLI add csv_file_ingestion    --auto-install
$CLI add imputation            --auto-install
$CLI add one_hot_encoding      --auto-install
$CLI add feature_scaler        --auto-install
$CLI add dataframe_to_parquet  --auto-install

echo ">>> Writing demo defs.yaml for each component"

cat > "src/$PKG/defs/csv_file_ingestion/defs.yaml" <<EOF
type: $PKG.components.csv_file_ingestion.component.CSVFileIngestionComponent
attributes:
  asset_name: penguins_raw
  file_path: https://raw.githubusercontent.com/allisonhorst/palmerpenguins/main/inst/extdata/penguins.csv
  description: Palmer Penguins dataset
  group_name: ingest
EOF

cat > "src/$PKG/defs/imputation/defs.yaml" <<EOF
type: $PKG.components.imputation.component.ImputationComponent
attributes:
  asset_name: penguins_imputed
  upstream_asset_key: penguins_raw
  strategy: mean
  columns: [bill_length_mm, bill_depth_mm, flipper_length_mm, body_mass_g]
  group_name: transform
EOF

cat > "src/$PKG/defs/one_hot_encoding/defs.yaml" <<EOF
type: $PKG.components.one_hot_encoding.component.OneHotEncodingComponent
attributes:
  asset_name: penguins_encoded
  upstream_asset_key: penguins_imputed
  columns: [species, island, sex]
  drop_first: false
  group_name: transform
EOF

cat > "src/$PKG/defs/feature_scaler/defs.yaml" <<EOF
type: $PKG.components.feature_scaler.component.FeatureScalerComponent
attributes:
  asset_name: penguins_scaled
  upstream_asset_key: penguins_encoded
  columns: [bill_length_mm, bill_depth_mm, flipper_length_mm, body_mass_g]
  strategy: standard
  group_name: transform
EOF

cat > "src/$PKG/defs/dataframe_to_parquet/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_parquet.component.DataframeToParquetComponent
attributes:
  asset_name: penguins_features
  upstream_asset_key: penguins_scaled
  file_path: /tmp/penguins_features.parquet
  compression: snappy
  index: false
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Materialize headlessly:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Or open the UI:
    cd $PROJECT_DIR && uv run dg dev   # http://localhost:3000

Output: /tmp/penguins_features.parquet — 344 rows × ~14 columns.
MSG
