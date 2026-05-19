#!/usr/bin/env bash
# Data combination demo — 7 transforms that combine, reshape, or
# coerce DataFrames.
#
# WHAT THIS DEMONSTRATES
#   The "join + reshape + coerce" family. Each transform takes one or
#   more upstreams and emits a derived DataFrame. Pure local, no deps
#   beyond pandas.
#
# Asset graph:
#   orders   (synthetic 30 rows)
#   customers (synthetic 10 rows)
#   q1_sales / q2_sales (synthetic split-quarter data)
#   tags_data (rows with list-typed columns)
#         │
#         ├── orders_with_customers   ← dataframe_join (left join orders + customers)
#         ├── all_sales               ← dataframe_union (q1 + q2)
#         ├── orders_with_metrics     ← formula (computed columns)
#         ├── orders_typed            ← type_coercer (string → int/float/datetime)
#         ├── orders_dates_parsed     ← datetime_parser (parse + extract components)
#         ├── exploded_tags           ← array_exploder (list column → row-per-element)
#         └── filled_sensors          ← ts_filler (forward-fill date gaps)
#
# COST: \$0 — pandas only.

set -euo pipefail
PROJECT_DIR="${1:-data-combination-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas numpy
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 7 combination transforms"
$CLI add dataframe_join   --auto-install
$CLI add dataframe_union  --auto-install
$CLI add formula          --auto-install
$CLI add type_coercer     --auto-install
$CLI add datetime_parser  --auto-install
$CLI add array_exploder   --auto-install
$CLI add ts_filler        --auto-install

echo ">>> Writing inline source assets"
mkdir -p "src/$PKG/defs/source_data"
cat > "src/$PKG/defs/source_data/__init__.py" <<'PYEOF'
import pandas as pd
import dagster as dg


@dg.asset(group_name="ingest")
def orders() -> pd.DataFrame:
    return pd.DataFrame([
        {"order_id": i, "customer_id": (i % 5) + 1, "list_price": 100.0,
         "price": 80.0 + (i % 10), "cost": 40.0, "quantity": (i % 4) + 1,
         "order_date_str": f"2025-04-{(i % 28) + 1:02d}",
         "tags_str": "['vip', 'priority']" if i % 3 == 0 else "['standard']",
         "active_str": "true" if i % 2 == 0 else "false"}
        for i in range(30)
    ])


@dg.asset(group_name="ingest")
def customers() -> pd.DataFrame:
    return pd.DataFrame([
        {"customer_id": i + 1, "name": f"Customer {chr(65 + i)}",
         "country": ["US", "CA", "UK", "DE", "FR"][i % 5]}
        for i in range(10)
    ])


@dg.asset(group_name="ingest")
def q1_sales() -> pd.DataFrame:
    return pd.DataFrame([
        {"sale_id": i, "quarter": "Q1", "amount": 100 + i} for i in range(15)
    ])


@dg.asset(group_name="ingest")
def q2_sales() -> pd.DataFrame:
    return pd.DataFrame([
        {"sale_id": i + 100, "quarter": "Q2", "amount": 150 + i} for i in range(15)
    ])


@dg.asset(group_name="ingest", description="Documents with list-typed tags column for array_exploder.")
def tags_data() -> pd.DataFrame:
    return pd.DataFrame([
        {"doc_id": i, "tags": ["vip", "priority"] if i % 3 == 0 else ["standard"]}
        for i in range(10)
    ])


@dg.asset(group_name="ingest", description="Sensor readings with date gaps for ts_filler.")
def raw_sensors() -> pd.DataFrame:
    # Intentional date gaps — only days 1, 2, 5, 7 represented
    rows = []
    for device in ("sensor_a", "sensor_b"):
        for day in (1, 2, 5, 7):
            rows.append({
                "device_id": device,
                "reading_date": f"2025-04-{day:02d}",
                "temperature": 70 + day,
                "humidity": 50 + day,
                "pressure": 1010 + day,
            })
    return pd.DataFrame(rows)


defs = dg.Definitions(assets=[orders, customers, q1_sales, q2_sales, tags_data, raw_sensors])
PYEOF

echo ">>> Writing 7 transform defs.yaml"

cat > "src/$PKG/defs/dataframe_join/defs.yaml" <<EOF
type: $PKG.components.dataframe_join.component.DataframeJoin
attributes:
  asset_name: orders_with_customers
  left_asset_key: orders
  right_asset_key: customers
  how: left
  "on": [customer_id]
  group_name: combined
EOF

cat > "src/$PKG/defs/dataframe_union/defs.yaml" <<EOF
type: $PKG.components.dataframe_union.component.DataframeUnion
attributes:
  asset_name: all_sales
  upstream_asset_keys: [q1_sales, q2_sales]
  ignore_index: true
  join: outer
  group_name: combined
EOF

cat > "src/$PKG/defs/formula/defs.yaml" <<EOF
type: $PKG.components.formula.component.FormulaComponent
attributes:
  asset_name: orders_with_metrics
  upstream_asset_key: orders
  expressions:
    total_revenue: "price * quantity"
    discount_amount: "list_price - price"
  group_name: combined
EOF

cat > "src/$PKG/defs/type_coercer/defs.yaml" <<EOF
type: $PKG.components.type_coercer.component.TypeCoercerComponent
attributes:
  asset_name: orders_typed
  upstream_asset_key: orders
  type_map:
    active_str: bool
    quantity: int
  errors: coerce
  group_name: combined
EOF

cat > "src/$PKG/defs/datetime_parser/defs.yaml" <<EOF
type: $PKG.components.datetime_parser.component.DatetimeParser
attributes:
  asset_name: orders_dates_parsed
  upstream_asset_key: orders
  date_column: order_date_str
  input_format: "%Y-%m-%d"
  output_column: order_date_parsed
  extract_components: true
  group_name: combined
EOF

cat > "src/$PKG/defs/array_exploder/defs.yaml" <<EOF
type: $PKG.components.array_exploder.component.ArrayExploderComponent
attributes:
  asset_name: exploded_tags
  upstream_asset_key: tags_data
  column: tags
  ignore_index: true
  drop_nulls: false
  group_name: combined
EOF

cat > "src/$PKG/defs/ts_filler/defs.yaml" <<EOF
type: $PKG.components.ts_filler.component.TsFillerComponent
attributes:
  asset_name: filled_sensors
  upstream_asset_key: raw_sensors
  date_column: reading_date
  frequency: D
  fill_method: forward_fill
  value_columns: [temperature, humidity, pressure]
  group_by: [device_id]
  group_name: combined
EOF

cat <<MSG

>>> Setup complete.

Materialize all 7 transforms + their sources:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Browse:
    uv run dg dev   # http://localhost:3000
MSG
