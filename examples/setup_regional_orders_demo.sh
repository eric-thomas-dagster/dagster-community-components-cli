#!/usr/bin/env bash
# Multi-region orders union demo — three regional CSVs → one global table.
#
# Three regions (NA, EU, APAC) export their order extracts as separate
# CSVs with slightly different column sets (NA uses USD, EU uses EUR,
# APAC includes a tax column the others don't). dataframe_union stacks
# them with `join: outer` so the missing columns become NaN, then
# dataframe_to_csv writes the unified extract.
#
# Pipeline (5 components, all autoloaded by `dg`):
#     file_ingestion x 3 → dataframe_union → dataframe_to_csv

set -euo pipefail

PROJECT_DIR="${1:-regional-orders-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas
uv add --dev -q dagster-dg-cli dagster-webserver

echo ">>> Generating three regional CSVs (different column sets on purpose)"
cat > /tmp/orders_na.csv <<'EOF'
order_id,customer_id,amount_usd,region
NA-001,c_2001,89.99,north_america
NA-002,c_2002,142.50,north_america
NA-003,c_2003,56.00,north_america
NA-004,c_2004,310.75,north_america
EOF

cat > /tmp/orders_eu.csv <<'EOF'
order_id,customer_id,amount_eur,region
EU-101,c_3101,72.30,europe
EU-102,c_3102,118.40,europe
EU-103,c_3103,45.00,europe
EU-104,c_3104,205.10,europe
EU-105,c_3105,67.85,europe
EOF

cat > /tmp/orders_apac.csv <<'EOF'
order_id,customer_id,amount_local,tax_rate,region
APAC-201,c_4201,8500.00,0.10,apac
APAC-202,c_4202,12300.00,0.10,apac
APAC-203,c_4203,4700.00,0.07,apac
EOF

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing components — file_ingestion x3 + union + sink"
$CLI add file_ingestion --auto-install --target-dir "src/$PKG/components/file_ingestion"
$CLI add dataframe_union    --auto-install
$CLI add dataframe_to_csv   --auto-install

# Three separate defs/ directories so each gets its own defs.yaml — the
# class file is shared (one components/file_ingestion/), the instance
# config differs.
mkdir -p "src/$PKG/defs/orders_na" "src/$PKG/defs/orders_eu" "src/$PKG/defs/orders_apac"

echo ">>> Writing demo defs.yaml for each component"

cat > "src/$PKG/defs/orders_na/defs.yaml" <<EOF
type: $PKG.components.file_ingestion.component.FileIngestionComponent
attributes:
  asset_name: orders_na
  file_path: /tmp/orders_na.csv
  description: North America regional order extract (USD)
  group_name: ingest
EOF

cat > "src/$PKG/defs/orders_eu/defs.yaml" <<EOF
type: $PKG.components.file_ingestion.component.FileIngestionComponent
attributes:
  asset_name: orders_eu
  file_path: /tmp/orders_eu.csv
  description: Europe regional order extract (EUR)
  group_name: ingest
EOF

cat > "src/$PKG/defs/orders_apac/defs.yaml" <<EOF
type: $PKG.components.file_ingestion.component.FileIngestionComponent
attributes:
  asset_name: orders_apac
  file_path: /tmp/orders_apac.csv
  description: APAC regional order extract (local currency + tax_rate)
  group_name: ingest
EOF

cat > "src/$PKG/defs/dataframe_union/defs.yaml" <<EOF
type: $PKG.components.dataframe_union.component.DataframeUnion
attributes:
  asset_name: orders_global
  upstream_asset_keys:
    - orders_na
    - orders_eu
    - orders_apac
  ignore_index: true
  join: outer
  group_name: union
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: orders_global_report
  upstream_asset_key: orders_global
  file_path: /tmp/orders_global.csv
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

Output: /tmp/orders_global.csv — 12 orders across 3 regions, with the
column union of all three sources (NA's amount_usd, EU's amount_eur,
APAC's amount_local + tax_rate). Missing values are NaN by region.

Inspect:
    cat /tmp/orders_global.csv
MSG
