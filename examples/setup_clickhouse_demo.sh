#!/usr/bin/env bash
# ClickHouse end-to-end demo — single-container Docker.
#
# WHAT THIS DEMONSTRATES
#   The three ClickHouse community components shipped in dagster-community-components:
#
#     1. `clickhouse_resource` — connection resource (clickhouse-connect HTTP client + SQLAlchemy URL)
#     2. `dataframe_to_clickhouse` — bulk-insert Pandas DataFrame via clickhouse-connect's insert_df
#     3. `external_clickhouse_table` — declare-only catalog entry for a ClickHouse table
#
#   + uses the existing `synthetic_data_generator` to create input rows.
#
# Asset graph after scaffold:
#
#   synthetic_data_generator (orders, ~10k rows)
#         │
#         ▼
#   dataframe_to_clickhouse (load into ClickHouse table)
#         │
#         ▼
#   external_clickhouse_table (catalog presence — ClickHouse owns lifecycle)
#
# COST: $0 — runs ClickHouse Server in Docker locally on port 8123 (HTTP) + 9000 (native).

set -euo pipefail
PROJECT_DIR="${1:-clickhouse-demo}"
CH_PORT_HTTP="${CH_PORT_HTTP:-18123}"   # 18123 to avoid colliding with a host install of ClickHouse
CH_PORT_NATIVE="${CH_PORT_NATIVE:-19000}"
CH_CONTAINER="${CH_CONTAINER:-clickhouse-demo-server}"

# --- 1. Bring up ClickHouse Server ---
echo ">>> Starting ClickHouse Server in Docker (container: $CH_CONTAINER)"
docker rm -f "$CH_CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$CH_CONTAINER" \
  -p "$CH_PORT_HTTP:8123" -p "$CH_PORT_NATIVE:9000" \
  -e CLICKHOUSE_USER=default \
  -e CLICKHOUSE_PASSWORD= \
  -e CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1 \
  --ulimit nofile=262144:262144 \
  clickhouse/clickhouse-server:latest >/dev/null

echo ">>> Waiting for ClickHouse to come up..."
for i in $(seq 1 30); do
  if curl -sS "http://localhost:$CH_PORT_HTTP/ping" 2>/dev/null | grep -q "Ok"; then
    echo "    ready after ${i}s"
    break
  fi
  sleep 1
done

# Pre-create the destination database + table.
# Use POST (DDL is rejected over GET since ClickHouse 22+ — GET implies readonly mode).
echo ">>> Creating destination database + table analytics.orders"
curl -sS -X POST "http://localhost:$CH_PORT_HTTP/" --data "CREATE DATABASE IF NOT EXISTS analytics"
curl -sS -X POST "http://localhost:$CH_PORT_HTTP/" --data "$(cat <<EOF
CREATE TABLE IF NOT EXISTS analytics.orders (
  order_id     String,
  customer_id  String,
  order_date   DateTime,
  category     String,
  num_items    Int64,
  subtotal     Float64,
  shipping     Float64,
  tax          Float64,
  total        Float64,
  status       String,
  region       String
) ENGINE = MergeTree()
ORDER BY (order_date, order_id)
EOF
)"
echo ""

# --- 2. Scaffold the Dagster project ---
echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q 'yarl<1.24' pandas requests clickhouse-connect
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing components (--refresh on first call busts the registry-manifest cache)"
$CLI --refresh add synthetic_data_generator --auto-install
$CLI add clickhouse_resource --auto-install
$CLI add dataframe_to_clickhouse --auto-install
$CLI add external_clickhouse_table --auto-install

echo ">>> Overwriting CLI-installed example defs.yamls with demo-specific config"

# `dagster-component add` installs to src/<pkg>/defs/<component>/defs.yaml and
# copies the example.yaml's placeholder values. We overwrite those with the
# demo's concrete configuration. Writing to NEW directories would leave the
# example.yamls active (with their placeholder upstream_asset_key values
# that don't exist in this demo), which breaks asset resolution.

# synthetic_data_generator → produce an `orders_clean` DataFrame.
cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: orders_clean
  schema_type: orders
  row_count: 10000
  group_name: source
EOF

# dataframe_to_clickhouse → load orders_clean into ClickHouse.
cat > "src/$PKG/defs/dataframe_to_clickhouse/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_clickhouse.component.DataframeToClickHouseComponent
attributes:
  asset_name: clickhouse_orders_load
  upstream_asset_key: orders_clean
  table: orders
  database: analytics
  host_env_var: CLICKHOUSE_HOST
  port: $CH_PORT_HTTP
  secure: false
  username_env_var: CLICKHOUSE_USER
  password_env_var: CLICKHOUSE_PASSWORD
  group_name: clickhouse
EOF

# external_clickhouse_table → declare the table in the catalog.
cat > "src/$PKG/defs/external_clickhouse_table/defs.yaml" <<EOF
type: $PKG.components.external_clickhouse_table.component.ExternalClickHouseTableComponent
attributes:
  asset_key: clickhouse/analytics/orders
  database: analytics
  table: orders
  host_env_var: CLICKHOUSE_HOST
  port: $CH_PORT_HTTP
  username_env_var: CLICKHOUSE_USER
  password_env_var: CLICKHOUSE_PASSWORD
  group_name: clickhouse
  description: |
    Orders table — loaded by dataframe_to_clickhouse; declared external
    so the catalog carries lineage from the Dagster-managed load asset.
EOF

# clickhouse_resource → connection resource (kept on default path).
cat > "src/$PKG/defs/clickhouse_resource/defs.yaml" <<EOF
type: $PKG.components.clickhouse_resource.component.ClickHouseResourceComponent
attributes:
  resource_key: clickhouse_resource
  host: localhost
  port: $CH_PORT_HTTP
  database: analytics
  username: default
  password: ""
  secure: false
EOF

# --- 3. Final instructions ---
echo ""
echo "============================================================"
echo "ClickHouse demo scaffolded at $PROJECT_DIR"
echo "============================================================"
echo ""
echo "ClickHouse Server is running on:"
echo "  HTTP    http://localhost:$CH_PORT_HTTP"
echo "  Native  tcp://localhost:$CH_PORT_NATIVE"
echo ""
echo "Quick check:"
echo "  curl 'http://localhost:$CH_PORT_HTTP/?query=SELECT+version()'"
echo ""
echo "Next steps:"
echo ""
echo "  cd $PROJECT_DIR"
echo "  export CLICKHOUSE_HOST=localhost"
echo "  export CLICKHOUSE_USER=default"
echo "  export CLICKHOUSE_PASSWORD="
echo ""
echo "  uv run dg dev                                              # UI at http://localhost:3000"
echo "  # OR headless smoke test:"
echo "  uv run dg launch --assets '*'"
echo ""
echo "Then verify:"
echo "  curl 'http://localhost:$CH_PORT_HTTP/?query=SELECT+count(*)+FROM+analytics.orders'"
echo ""
echo "Cleanup:"
echo "  docker rm -f $CH_CONTAINER"
echo ""
