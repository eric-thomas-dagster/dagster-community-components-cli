#!/usr/bin/env bash
# Local sinks demo — write the same DataFrame to 5 file/table formats.
#
# WHAT THIS DEMONSTRATES
#   The local-runnable sink family. The same upstream `orders` DataFrame
#   fans out to 5 sinks, each writing the same data in a different format.
#   Useful as a quick visual confirmation that your upstream data round-trips
#   through every supported format.
#
# Components covered (5):
#   - dataframe_to_csv      → $PROJECT_ABS/out/local_sinks_demo/orders.csv
#   - dataframe_to_parquet  → $PROJECT_ABS/out/local_sinks_demo/orders.parquet
#   - dataframe_to_json     → $PROJECT_ABS/out/local_sinks_demo/orders.json
#   - dataframe_to_excel    → $PROJECT_ABS/out/local_sinks_demo/orders.xlsx
#   - dataframe_to_table    → SQLite at $PROJECT_ABS/out/local_sinks_demo/orders.db
#                              (table name: orders)
#
# Cloud-backed sinks (dataframe_to_s3, _gcs, _adls, _bigquery,
# _snowflake, _redshift, _databricks, _fabric_lakehouse, _dynatrace_events,
# _newrelic_logs, _otlp_*, _prometheus, _servicebus, _eventhub, _kusto)
# need real credentials and are not exercised here.
#
# COST: \$0 — fully local.

set -euo pipefail
PROJECT_DIR="${1:-local-sinks-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"
mkdir -p out
PKG="$(ls src/ | head -1)"

uv add -q pandas pyarrow openpyxl sqlalchemy
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 5 sink components"
$CLI add dataframe_to_csv      --auto-install
$CLI add dataframe_to_parquet  --auto-install
$CLI add dataframe_to_json     --auto-install
$CLI add dataframe_to_excel    --auto-install
$CLI add dataframe_to_table    --auto-install

echo ">>> Writing inline source asset"
mkdir -p "src/$PKG/defs/source_data"
cat > "src/$PKG/defs/source_data/__init__.py" <<'PYEOF'
import os
import pandas as pd
import dagster as dg

# SQLAlchemy URL for the dataframe_to_table sink (SQLite is dependency-free)
os.environ.setdefault(
    "ORDERS_DB_URL",
    "sqlite:///$PROJECT_ABS/out/local_sinks_demo/orders.db",
)


@dg.asset(group_name="ingest", description="30 synthetic orders — fanned out to 5 local sinks")
def orders() -> pd.DataFrame:
    rows = []
    for i in range(30):
        rows.append({
            "order_id": 1000 + i,
            "customer_id": (i % 10) + 1,
            "amount": round(50 + i * 1.5, 2),
            "status": ["pending", "shipped", "delivered"][i % 3],
            "order_date": f"2025-04-{(i % 28) + 1:02d}",
        })
    return pd.DataFrame(rows)


defs = dg.Definitions(assets=[orders])
PYEOF

mkdir -p $PROJECT_ABS/out/local_sinks_demo

echo ">>> Writing 5 sink defs.yaml"

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: orders_csv
  upstream_asset_key: orders
  file_path: out/local_sinks_demo/orders.csv
  group_name: sinks
EOF

cat > "src/$PKG/defs/dataframe_to_parquet/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_parquet.component.DataframeToParquetComponent
attributes:
  asset_name: orders_parquet
  upstream_asset_key: orders
  file_path: out/local_sinks_demo/orders.parquet
  group_name: sinks
EOF

cat > "src/$PKG/defs/dataframe_to_json/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_json.component.DataframeToJsonComponent
attributes:
  asset_name: orders_json
  upstream_asset_key: orders
  file_path: out/local_sinks_demo/orders.json
  group_name: sinks
EOF

cat > "src/$PKG/defs/dataframe_to_excel/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_excel.component.DataframeToExcelComponent
attributes:
  asset_name: orders_xlsx
  upstream_asset_key: orders
  file_path: out/local_sinks_demo/orders.xlsx
  group_name: sinks
EOF

cat > "src/$PKG/defs/dataframe_to_table/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_table.component.DataframeToTableComponent
attributes:
  asset_name: orders_table
  upstream_asset_key: orders
  database_url_env_var: ORDERS_DB_URL
  table_name: orders
  if_exists: replace
  group_name: sinks
EOF

cat <<MSG

>>> Setup complete.

Materialize all 5 sinks (one upstream, five outputs):
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Inspect:
    ls $PROJECT_ABS/out/local_sinks_demo/
        orders.csv  orders.parquet  orders.json  orders.xlsx  orders.db

    sqlite3 $PROJECT_ABS/out/local_sinks_demo/orders.db "SELECT count(*) FROM orders;"
        # 30

Or open the asset graph:
    uv run dg dev   # http://localhost:3000
MSG
