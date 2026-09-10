#!/usr/bin/env bash
# sql_observation_sensor demo — end-to-end against DuckDB.
#
# Proves the sensor's native SQLAlchemy path works: connect to a
# real database, SELECT COUNT(*) + optional MAX(watermark_column),
# emit AssetMaterialization with a data_version tag. DuckDB is the
# ideal target — SQLAlchemy-compatible via duckdb-engine, no server,
# no auth, hermetic (file on disk).
#
# Shape (3 community components, no custom Python):
#   external_sql_asset (declare-only Dagster asset for the SQL table)
#     └── sql_observation_sensor (polls the table + emits materialization)
#
# Also seeds a small DuckDB file with a `sales_orders` table so the
# sensor has something to observe on first tick.

set -euo pipefail
PROJECT_DIR="${1:-sql-observation-sensor-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"
mkdir -p out
PKG="$(ls src/ | head -1)"

echo ">>> Adding deps"
uv add -q 'sqlalchemy>=2.0.0' 'duckdb>=1.0.0' 'duckdb-engine>=0.13.0'
uv add -q 'yarl<1.24'   # workaround: yarl 1.24.0 only ships cp310 wheels
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --refresh --from dagster-community-components-cli dagster-component --refresh"

echo ">>> Installing 2 community components"
$CLI add external_sql_asset      --auto-install
$CLI add sql_observation_sensor  --auto-install

# ── Seed a DuckDB file with a small orders table + a MAX(updated_at)
#    watermark column so the sensor can prove its watermark path.
DUCKDB_PATH="$PROJECT_ABS/out/orders.duckdb"
DUCKDB_URL="duckdb:///$DUCKDB_PATH"
echo ">>> Seeding $DUCKDB_PATH"
uv run python - <<PY
import duckdb
conn = duckdb.connect("$DUCKDB_PATH")
conn.execute("DROP TABLE IF EXISTS sales_orders")
conn.execute("""
CREATE TABLE sales_orders (
    order_id     INTEGER PRIMARY KEY,
    customer     VARCHAR,
    amount_usd   DOUBLE,
    updated_at   TIMESTAMP
);
""")
conn.execute("""
INSERT INTO sales_orders VALUES
    (1, 'Acme',    120.00, '2026-09-10 09:00:00'),
    (2, 'Widgets', 240.00, '2026-09-10 09:05:00'),
    (3, 'Cogs',     87.50, '2026-09-10 09:12:00');
""")
row_count = conn.execute("SELECT COUNT(*) FROM sales_orders").fetchone()[0]
max_ts    = conn.execute("SELECT MAX(updated_at) FROM sales_orders").fetchone()[0]
print(f"    seeded {row_count} orders, latest watermark = {max_ts}")
PY

echo ">>> Writing defs.yaml"

# The external asset — declare-only, no compute. The sensor observes
# this asset key and emits AssetMaterialization onto it.
cat > "src/$PKG/defs/external_sql_asset/defs.yaml" <<EOF
type: $PKG.components.external_sql_asset.component.ExternalSqlAsset
attributes:
  asset_key: orders/sales_orders
  table_name: sales_orders
  connection_string_env_var: DUCKDB_URL
  group_name: sql_sources
  description: External DuckDB table observed by sql_observation_sensor
EOF

# The sensor — points at the external asset key + connection string
# env var. Emits AssetMaterialization every 60s (short interval so the
# demo shows movement quickly; production would be 5-15 minutes).
cat > "src/$PKG/defs/sql_observation_sensor/defs.yaml" <<EOF
type: $PKG.components.sql_observation_sensor.component.SqlObservationSensorComponent
attributes:
  sensor_name: sales_orders_health
  asset_key: orders/sales_orders
  table_name: sales_orders
  connection_string_env_var: DUCKDB_URL
  watermark_column: updated_at
  check_interval_seconds: 60
  emit_materialization: true
EOF

cat <<MSG

>>> Setup complete.

Set the connection string env var (points sqlalchemy at the seeded DuckDB file):

    export DUCKDB_URL='$DUCKDB_URL'

Verify the sensor loads:

    cd $PROJECT_DIR && uv run dg check defs

Run the sensor once (headless, prints the materialization event):

    cd $PROJECT_DIR && DUCKDB_URL='$DUCKDB_URL' \\
        uv run dagster sensor cursor sales_orders_health --clear 2>/dev/null || true
    cd $PROJECT_DIR && DUCKDB_URL='$DUCKDB_URL' \\
        uv run dagster asset materialize --select 'orders/sales_orders'

    # Or start the UI and toggle the sensor on:
    #   cd $PROJECT_DIR && DUCKDB_URL='$DUCKDB_URL' uv run dg dev
    #   Sensors tab → sales_orders_health → toggle on
    #   Assets tab → orders/sales_orders → asset tile goes green each tick

Watch the data_version change when the table advances:

    uv run python -c "
import duckdb
conn = duckdb.connect('$DUCKDB_PATH')
conn.execute(\"INSERT INTO sales_orders VALUES (99, 'NewCo', 999.00, '2026-09-10 12:00:00')\")
print('inserted row; sensor will re-fire on next tick with new data_version')
"
MSG
