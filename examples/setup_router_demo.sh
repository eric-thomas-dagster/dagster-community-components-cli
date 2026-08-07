#!/usr/bin/env bash
# Router demo — multi-output conditional split.
#
# 30 synthetic orders → router splits into 3 buckets by total:
#   high   (total > 1000)        → ~few rows
#   medium (100 ≤ total ≤ 1000)  → ~most rows
#   low    (default)              → ~remainder
# Each bucket flows to its own CSV sink.
#
# Pipeline:
#                     ┌─→ high_value_orders   → CSV
#   csv → router ─────┼─→ medium_value_orders → CSV
#                     └─→ low_value_orders    → CSV

set -euo pipefail
PROJECT_DIR="${1:-router-demo}"

echo ">>> Scaffolding"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"
mkdir -p out
PKG="$(ls src/ | head -1)"

uv add -q pandas
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

mkdir -p $PROJECT_ABS/out/router_demo
# Synthetic orders now generated 100%-components via parametric_data_generator.

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing components"
$CLI add parametric_data_generator --auto-install
$CLI add router                    --auto-install
$CLI add dataframe_to_csv          --auto-install
mkdir -p "src/$PKG/defs/csv_med"  # only needs defs.yaml; component code is in components/dataframe_to_csv/
mkdir -p "src/$PKG/defs/csv_low"  # only needs defs.yaml; component code is in components/dataframe_to_csv/
echo ">>> Writing demo defs.yaml"

mkdir -p "src/$PKG/defs/orders"
cat > "src/$PKG/defs/orders/defs.yaml" <<EOF
type: $PKG.components.parametric_data_generator.component.ParametricDataGeneratorComponent
attributes:
  asset_name: orders
  row_count: 30
  random_state: 7
  description: 30 synthetic orders, biased across low/medium/high totals
  group_name: router_demo
  columns:
    order_id:
      type: id
      prefix: "ORD"
      width: 4
    customer_id:
      type: id
      prefix: "C"
      width: 3
    bucket:
      type: choice
      values: [low, medium, high]
    low_v:
      type: float
      min: 20
      max: 99
      precision: 2
    med_v:
      type: float
      min: 100
      max: 1000
      precision: 2
    high_v:
      type: float
      min: 1001
      max: 5000
      precision: 2
    total:
      type: formula
      formula: "low_v if bucket == 'low' else (med_v if bucket == 'medium' else high_v)"
EOF

# Suppress the auto-installed example defs that would conflict
rm -rf "src/$PKG/defs/parametric_data_generator" "src/$PKG/defs/router" "src/$PKG/defs/dataframe_to_csv"

mkdir -p "src/$PKG/defs/router" "src/$PKG/defs/dataframe_to_csv"
cat > "src/$PKG/defs/router/defs.yaml" <<EOF
type: $PKG.components.router.component.RouterComponent
attributes:
  upstream_asset_key: orders
  routes:
    - asset_name: high_value_orders
      condition: "total > 1000"
    - asset_name: medium_value_orders
      condition: "total >= 100 and total <= 1000"
  default_asset_name: low_value_orders
  exclusive: true
  group_name: router_demo
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: high_report
  upstream_asset_key: high_value_orders
  file_path: out/router_demo/high.csv
  include_index: false
  group_name: router_demo
EOF

cat > "src/$PKG/defs/csv_med/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: medium_report
  upstream_asset_key: medium_value_orders
  file_path: out/router_demo/medium.csv
  include_index: false
  group_name: router_demo
EOF

cat > "src/$PKG/defs/csv_low/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: low_report
  upstream_asset_key: low_value_orders
  file_path: out/router_demo/low.csv
  include_index: false
  group_name: router_demo
EOF

cat <<MSG

>>> Setup complete.
Materialize: cd $PROJECT_DIR && uv run dg launch --assets '*'
Outputs: $PROJECT_ABS/out/router_demo/{high,medium,low}.csv
MSG
