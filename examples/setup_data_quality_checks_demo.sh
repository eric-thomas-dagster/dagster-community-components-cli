#!/usr/bin/env bash
# Data Quality Checks demo — exercise enhanced_data_quality_checks +
# pandas_dataframe_check + pandera_asset_check + freshness_check on a
# single asset.
#
# WHAT THIS DEMONSTRATES
#   100 synthetic e-commerce orders → CSV. Four parallel asset_check
#   components verify the asset:
#     - enhanced_data_quality_checks: row count, null, range, data type,
#       anomaly detection (z_score on row count)
#     - pandas_dataframe_check: required columns + dtype enforcement
#     - pandera_asset_check: Pandera schema with column-level validators
#     - freshness_check: SLA on materialization recency
#
#   All four checks run on the same orders_raw asset, so users can see
#   them side-by-side in the Dagster UI checks panel.
#
# Pipeline:
#   synthetic_data_generator → orders_raw → dataframe_to_csv
#                                 ▲
#                                 ├── enhanced_data_quality_checks
#                                 ├── pandas_dataframe_check
#                                 ├── pandera_asset_check
#                                 └── freshness_check
#
# COST: $0 — entirely local

set -euo pipefail
PROJECT_DIR="${1:-data-quality-checks-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas pandera dagster-pandas
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 6 community components"
$CLI add synthetic_data_generator         --auto-install
$CLI add dataframe_to_csv                 --auto-install
$CLI add enhanced_data_quality_checks     --auto-install
$CLI add pandas_dataframe_check           --auto-install
$CLI add pandera_asset_check              --auto-install
$CLI add freshness_check                  --auto-install

# A small pandera schema module that the pandera_asset_check references
mkdir -p "src/$PKG/schemas"
cat > "src/$PKG/schemas/__init__.py" <<EOF
import pandera.pandas as pa

orders_schema = pa.DataFrameSchema({
    "order_id":    pa.Column(str, nullable=False, unique=True),
    "customer_id": pa.Column(str, nullable=False),
    "category":    pa.Column(str, pa.Check.isin(["Electronics", "Books", "Clothing", "Sports", "Toys", "Food", "Beauty"])),
    "num_items":   pa.Column(int, pa.Check.in_range(1, 100)),
    "subtotal":    pa.Column(float, pa.Check.greater_than(0)),
    "shipping":    pa.Column(float, pa.Check.greater_than_or_equal_to(0)),
    "tax":         pa.Column(float, pa.Check.greater_than_or_equal_to(0)),
    "total":       pa.Column(float, pa.Check.greater_than(0)),
    "status":      pa.Column(str, pa.Check.isin(["pending", "shipped", "delivered", "cancelled"])),
})
EOF

echo ">>> Writing demo defs.yaml"

cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: orders_raw
  schema_type: orders
  row_count: 100
  random_state: 42
  group_name: ingest
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: orders_csv
  upstream_asset_key: orders_raw
  file_path: /tmp/dq_orders.csv
  group_name: report
EOF

cat > "src/$PKG/defs/enhanced_data_quality_checks/defs.yaml" <<EOF
type: $PKG.components.enhanced_data_quality_checks.component.EnhancedDataQualityChecksComponent
attributes:
  assets:
    orders_raw:
      row_count_check:
        - name: orders_row_count
          min_rows: 50
          max_rows: 1000
          blocking: false
      null_check:
        - name: orders_critical_not_null
          columns: [order_id, customer_id, total]
          blocking: false
      range_check:
        - name: orders_total_in_range
          columns:
            - column: total
              min_value: 0
              max_value: 100000
          blocking: false
      data_type_check:
        - name: orders_dtype
          columns:
            - column: total
              expected_type: float
            - column: status
              expected_type: object
          blocking: false
EOF

cat > "src/$PKG/defs/pandas_dataframe_check/defs.yaml" <<EOF
type: $PKG.components.pandas_dataframe_check.component.PandasDataframeCheckComponent
attributes:
  asset_key: orders_raw
  required_columns: [order_id, customer_id, category, total, status]
  column_types:
    order_id: object
    total: float64
  blocking: false
EOF

cat > "src/$PKG/defs/pandera_asset_check/defs.yaml" <<EOF
type: $PKG.components.pandera_asset_check.component.PanderaAssetCheckComponent
attributes:
  asset_key: orders_raw
  schema_module: $PKG.schemas
  schema_name: orders_schema
  blocking: false
EOF

cat > "src/$PKG/defs/freshness_check/defs.yaml" <<EOF
type: $PKG.components.freshness_check.component.FreshnessPolicyComponent
attributes:
  asset_key: orders_raw
  policy_type: time_window
  fail_window_hours: 24.0
  warn_window_hours: 1.0
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

The pipeline will:
  1. Materialize orders_raw (100 synthetic orders)
  2. Run all 4 asset checks against orders_raw in parallel
  3. Write the CSV report
  4. Show check results in dg dev UI

View in UI:
    uv run dg dev   # then open http://localhost:3000 → Assets → orders_raw → Checks
MSG
