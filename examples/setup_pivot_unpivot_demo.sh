#!/usr/bin/env bash
# Pivot ↔ unpivot round-trip.
#
# Synthetic monthly sales by region (long format) → pivot to wide → unpivot
# back to long. Verifies the two transforms compose cleanly and lose no data.
#
# Pipeline:
#   csv (long) → pivot (wide) → unpivot (long-again) → CSVs

set -euo pipefail
PROJECT_DIR="${1:-pivot-unpivot-demo}"

echo ">>> Scaffolding"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas tabulate
uv add --dev -q dagster-dg-cli dagster-webserver

mkdir -p /tmp/pivot_demo
cat > /tmp/pivot_demo/sales_long.csv <<'EOF'
month,region,revenue
Jan,East,100
Jan,West,200
Jan,North,150
Feb,East,120
Feb,West,210
Feb,North,160
Mar,East,140
Mar,West,220
Mar,North,170
EOF

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 4 community components"
$CLI add file_ingestion --auto-install
$CLI add pivot              --auto-install
$CLI add unpivot            --auto-install
$CLI add dataframe_to_csv   --auto-install
mkdir -p "src/$PKG/defs/csv_long_again"  # only needs defs.yaml; component code is in components/dataframe_to_csv/
echo ">>> Writing demo defs.yaml"

cat > "src/$PKG/defs/file_ingestion/defs.yaml" <<EOF
type: $PKG.components.file_ingestion.component.FileIngestionComponent
attributes:
  asset_name: sales_long
  file_path: /tmp/pivot_demo/sales_long.csv
  description: 9 rows of monthly revenue by region (long format)
  group_name: pivot_demo
EOF

cat > "src/$PKG/defs/pivot/defs.yaml" <<EOF
type: $PKG.components.pivot.component.PivotComponent
attributes:
  asset_name: sales_wide
  upstream_asset_key: sales_long
  index_columns:
    - month
  pivot_column: region
  value_column: revenue
  agg_func: sum
  fill_value: 0
  group_name: pivot_demo
EOF

cat > "src/$PKG/defs/unpivot/defs.yaml" <<EOF
type: $PKG.components.unpivot.component.UnpivotComponent
attributes:
  asset_name: sales_long_again
  upstream_asset_key: sales_wide
  id_columns:
    - month
  var_name: region
  value_name: revenue
  drop_null_values: false
  group_name: pivot_demo
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: wide_report
  upstream_asset_key: sales_wide
  file_path: /tmp/pivot_demo/sales_wide.csv
  include_index: false
  group_name: pivot_demo
EOF

cat > "src/$PKG/defs/csv_long_again/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: long_report
  upstream_asset_key: sales_long_again
  file_path: /tmp/pivot_demo/sales_long_again.csv
  include_index: false
  group_name: pivot_demo
EOF

cat <<MSG

>>> Setup complete. Materialize:
    cd $PROJECT_DIR && uv run dg launch --assets '*'

Outputs:
    /tmp/pivot_demo/sales_wide.csv         (3 rows, 4 cols: month, East, North, West)
    /tmp/pivot_demo/sales_long_again.csv   (9 rows, 3 cols: month, region, revenue)
MSG
