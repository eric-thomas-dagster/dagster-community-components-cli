#!/usr/bin/env bash
# Titanic data-quality demo — canonical create-dagster + dg.
#
# Same Titanic CSV the simplest demo uses, but routed through three
# data-quality steps: cleanse strings, dedupe, winsorize fare outliers.
# Demonstrates the cleanup-pipeline shape — every dataset needs some
# variant of this before downstream analytics.
#
# Pipeline (5 components, all autoloaded by `dg`):
#     csv_file_ingestion → data_cleansing → unique_dedup → outlier_clipper → dataframe_to_csv

set -euo pipefail

PROJECT_DIR="${1:-titanic-quality-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas requests
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 5 community components into src/$PKG/components/ + defs/"
$CLI add csv_file_ingestion    --auto-install
$CLI add data_cleansing        --auto-install
$CLI add unique_dedup          --auto-install
$CLI add outlier_clipper       --auto-install
$CLI add dataframe_to_csv      --auto-install

echo ">>> Writing demo defs.yaml for each component"

# 1. Ingest — Titanic
cat > "src/$PKG/defs/csv_file_ingestion/defs.yaml" <<EOF
type: $PKG.components.csv_file_ingestion.component.CSVFileIngestionComponent
attributes:
  asset_name: passengers_raw
  file_path: https://raw.githubusercontent.com/datasciencedojo/datasets/master/titanic.csv
  description: Titanic — known to have missing Age, Cabin, Embarked + fare outliers
  group_name: ingest
EOF

# 2. Cleanse strings — trim whitespace, fill nulls in string columns
cat > "src/$PKG/defs/data_cleansing/defs.yaml" <<EOF
type: $PKG.components.data_cleansing.component.DataCleansingComponent
attributes:
  asset_name: passengers_clean
  upstream_asset_key: passengers_raw
  trim_whitespace: true
  null_handling: fill
  null_fill_value: "unknown"
  columns: [Name, Sex, Cabin, Embarked]
  group_name: quality
EOF

# 3. Dedupe on PassengerId — keeps first
cat > "src/$PKG/defs/unique_dedup/defs.yaml" <<EOF
type: $PKG.components.unique_dedup.component.UniqueDedupComponent
attributes:
  asset_name: passengers_unique
  upstream_asset_key: passengers_clean
  subset: [PassengerId]
  keep: first
  output_mode: unique
  group_name: quality
EOF

# 4. Clip Fare outliers — Titanic fares range from 0 to ~512; winsorize the
#    top + bottom whiskers using IQR (default 1.5x multiplier)
cat > "src/$PKG/defs/outlier_clipper/defs.yaml" <<EOF
type: $PKG.components.outlier_clipper.component.OutlierClipperComponent
attributes:
  asset_name: passengers_clipped
  upstream_asset_key: passengers_unique
  strategy: iqr
  action: clip
  columns: [Fare]
  iqr_multiplier: 1.5
  group_name: quality
EOF

# 5. Sink
cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: passengers_report
  upstream_asset_key: passengers_clipped
  file_path: /tmp/titanic_clean.csv
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

Output: /tmp/titanic_clean.csv — 891 passengers (no dupes), string nulls
filled with "unknown", Fare outliers clipped to the 1.5×IQR whiskers.

Inspect:
    head -3 /tmp/titanic_clean.csv
    awk -F, 'NR>1{print \$10}' /tmp/titanic_clean.csv | sort -n | tail -5
        # max Fare drops from \$512 to ~\$66 after clipping
MSG
