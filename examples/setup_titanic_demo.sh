#!/usr/bin/env bash
# Titanic demo — canonical create-dagster + dg layout.
#
# Pulls a public Titanic CSV, filters to first-class passengers, summarizes
# survival rate by gender, writes the result to /tmp/survival_report.csv.
#
# Pipeline (4 components, all autoloaded by `dg`):
#     csv_file_ingestion → filter → summarize → dataframe_to_csv

set -euo pipefail

PROJECT_DIR="${1:-titanic-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
# Read the package name back from the scaffold rather than predicting it
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas requests
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 4 community components into src/$PKG/components/ + defs/"
$CLI add csv_file_ingestion --auto-install
$CLI add filter             --auto-install
$CLI add summarize          --auto-install
$CLI add dataframe_to_csv   --auto-install

echo ">>> Writing demo defs.yaml for each component"

cat > "src/$PKG/defs/csv_file_ingestion/defs.yaml" <<EOF
type: $PKG.components.csv_file_ingestion.component.CSVFileIngestionComponent
attributes:
  asset_name: titanic_raw
  file_path: https://raw.githubusercontent.com/datasciencedojo/datasets/master/titanic.csv
  description: Public Titanic passenger data
  group_name: ingest
EOF

cat > "src/$PKG/defs/filter/defs.yaml" <<EOF
type: $PKG.components.filter.component.FilterComponent
attributes:
  asset_name: first_class_passengers
  upstream_asset_key: titanic_raw
  condition: "Pclass == 1"
  group_name: transform
EOF

cat > "src/$PKG/defs/summarize/defs.yaml" <<EOF
type: $PKG.components.summarize.component.SummarizeComponent
attributes:
  asset_name: survival_by_sex
  upstream_asset_key: first_class_passengers
  group_by: [Sex]
  aggregations:
    Survived: mean
    PassengerId: count
  group_name: transform
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: survival_report
  upstream_asset_key: survival_by_sex
  file_path: /tmp/survival_report.csv
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Materialize headlessly (one-shot):
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Or open the Dagster UI:
    cd $PROJECT_DIR
    uv run dg dev   # then visit http://localhost:3000

Output: /tmp/survival_report.csv

    Sex,Survived,PassengerId
    female,0.968,94
    male,0.369,122

(96.8% female survival rate, 36.9% male survival rate, in 1st class.)
MSG
