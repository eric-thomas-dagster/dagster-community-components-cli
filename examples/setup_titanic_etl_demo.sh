#!/usr/bin/env bash
# Kitchen-sink Titanic ETL demo — 9 transforms in one chain.
#
# Walks raw Titanic CSV through a real "data engineer's morning"
# pipeline: type coercion, string cleanup, median imputation, age
# bucketing, column rename + reorder, sample preview, final CSV.
#
# Pipeline (10 components, all autoloaded by `dg`):
#   csv_file_ingestion → type_coercer → data_cleansing → imputation
#                      → tile_binning → field_mapper → arrange
#                      → sample → dataframe_to_csv

set -euo pipefail

PROJECT_DIR="${1:-titanic-etl-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas requests
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 9 community components into src/$PKG/defs/"
$CLI add csv_file_ingestion    --auto-install
$CLI add type_coercer          --auto-install
$CLI add data_cleansing        --auto-install
$CLI add imputation            --auto-install
$CLI add tile_binning          --auto-install
$CLI add field_mapper          --auto-install
$CLI add arrange               --auto-install
$CLI add sample                --auto-install
$CLI add dataframe_to_csv      --auto-install

echo ">>> Writing demo defs.yaml for each component"

# 1. Ingest
cat > "src/$PKG/defs/csv_file_ingestion/defs.yaml" <<EOF
type: $PKG.components.csv_file_ingestion.component.CSVFileIngestionComponent
attributes:
  asset_name: titanic_raw
  file_path: https://raw.githubusercontent.com/datasciencedojo/datasets/master/titanic.csv
  description: Public Titanic passenger data
  group_name: ingest
EOF

# 2. Type coercion — Age + Fare numeric, Pclass int
cat > "src/$PKG/defs/type_coercer/defs.yaml" <<EOF
type: $PKG.components.type_coercer.component.TypeCoercerComponent
attributes:
  asset_name: titanic_typed
  upstream_asset_key: titanic_raw
  type_map:
    Age: float
    Fare: float
    Pclass: int
  errors: coerce
  group_name: clean
EOF

# 3. Trim whitespace + normalize casing on string cols
cat > "src/$PKG/defs/data_cleansing/defs.yaml" <<EOF
type: $PKG.components.data_cleansing.component.DataCleansingComponent
attributes:
  asset_name: titanic_clean
  upstream_asset_key: titanic_typed
  trim_whitespace: true
  null_handling: fill
  null_fill_value: "unknown"
  columns: [Name, Sex, Ticket, Cabin, Embarked]
  group_name: clean
EOF

# 4. Median-impute the (now numeric) Age + Fare
cat > "src/$PKG/defs/imputation/defs.yaml" <<EOF
type: $PKG.components.imputation.component.ImputationComponent
attributes:
  asset_name: titanic_imputed
  upstream_asset_key: titanic_clean
  strategy: median
  columns: [Age, Fare]
  group_name: clean
EOF

# 5. Bucket Age into age_bracket (Child / Teen / Adult / Senior)
cat > "src/$PKG/defs/tile_binning/defs.yaml" <<EOF
type: $PKG.components.tile_binning.component.TileBinningComponent
attributes:
  asset_name: titanic_binned
  upstream_asset_key: titanic_imputed
  column: Age
  method: custom
  bin_edges: [0, 13, 20, 60, 100]
  labels: [Child, Teen, Adult, Senior]
  output_column: age_bracket
  include_numeric_label: false
  group_name: enrich
EOF

# 6. Rename to snake_case (and drop the columns we don't want)
cat > "src/$PKG/defs/field_mapper/defs.yaml" <<EOF
type: $PKG.components.field_mapper.component.FieldMapperComponent
attributes:
  asset_name: titanic_mapped
  upstream_asset_key: titanic_binned
  mapping:
    PassengerId: passenger_id
    Survived: survived
    Pclass: pclass
    Name: name
    Sex: sex
    Age: age
    SibSp: siblings_spouses
    Parch: parents_children
    Ticket: ticket
    Fare: fare
    Cabin: cabin
    Embarked: embarked_port
    age_bracket: age_bracket
  drop_unmapped: true
  group_name: rename
EOF

# 7. Reorder columns: identity + outcome up front, derived/categorical later
cat > "src/$PKG/defs/arrange/defs.yaml" <<EOF
type: $PKG.components.arrange.component.ArrangeComponent
attributes:
  asset_name: titanic_arranged
  upstream_asset_key: titanic_mapped
  move_to_front: [passenger_id, survived, pclass, age_bracket]
  move_to_back: [cabin, ticket]
  group_name: rename
EOF

# 8. Take a 50-row reproducible sample for the preview file
cat > "src/$PKG/defs/sample/defs.yaml" <<EOF
type: $PKG.components.sample.component.SampleComponent
attributes:
  asset_name: titanic_sample
  upstream_asset_key: titanic_arranged
  n: 50
  random_state: 42
  group_name: preview
EOF

# 9. Sink — preview CSV
cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: titanic_preview_report
  upstream_asset_key: titanic_sample
  file_path: /tmp/titanic_preview.csv
  include_index: false
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Materialize the whole 9-step chain:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Output: /tmp/titanic_preview.csv — 50-row reproducible sample, fully
typed, imputed, bucketed, renamed, and reordered.

Inspect:
    head -5 /tmp/titanic_preview.csv
MSG
