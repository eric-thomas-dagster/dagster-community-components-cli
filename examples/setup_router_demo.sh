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
PKG="$(ls src/ | head -1)"

uv add -q pandas
uv add --dev -q dagster-dg-cli dagster-webserver

mkdir -p /tmp/router_demo
echo ">>> Generating 30 synthetic orders"
uv run python - <<'PY'
import csv, random
random.seed(7)
rows = []
for i in range(1, 31):
    total = round(random.choice([
        random.uniform(20, 99),       # low
        random.uniform(100, 1000),    # medium
        random.uniform(1001, 5000),   # high
    ]), 2)
    rows.append({"order_id": f"ORD{i:04d}", "customer_id": f"C{i:03d}", "total": total})
with open("/tmp/router_demo/orders.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=rows[0].keys())
    w.writeheader(); w.writerows(rows)
print(f"wrote {len(rows)} orders")
PY

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing components"
$CLI add csv_file_ingestion --auto-install
$CLI add router             --auto-install
$CLI add dataframe_to_csv   --auto-install
$CLI add dataframe_to_csv   --auto-install --target-dir "src/$PKG/defs/csv_med"
$CLI add dataframe_to_csv   --auto-install --target-dir "src/$PKG/defs/csv_low"

echo ">>> Writing demo defs.yaml"

cat > "src/$PKG/defs/csv_file_ingestion/defs.yaml" <<EOF
type: $PKG.components.csv_file_ingestion.component.CSVFileIngestionComponent
attributes:
  asset_name: orders
  file_path: /tmp/router_demo/orders.csv
  description: 30 synthetic orders with mixed totals
  group_name: router_demo
EOF

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
  file_path: /tmp/router_demo/high.csv
  include_index: false
  group_name: router_demo
EOF

cat > "src/$PKG/defs/csv_med/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: medium_report
  upstream_asset_key: medium_value_orders
  file_path: /tmp/router_demo/medium.csv
  include_index: false
  group_name: router_demo
EOF

cat > "src/$PKG/defs/csv_low/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: low_report
  upstream_asset_key: low_value_orders
  file_path: /tmp/router_demo/low.csv
  include_index: false
  group_name: router_demo
EOF

cat <<MSG

>>> Setup complete.
Materialize: cd $PROJECT_DIR && uv run dg launch --assets '*'
Outputs: /tmp/router_demo/{high,medium,low}.csv
MSG
