#!/usr/bin/env bash
# setup_cube_query_demo.sh
#
# Simple Cube semantic-layer demo. Spins up a local Cube dev server via
# Docker with sample e-commerce data, then materializes governed metrics
# as Dagster assets via cube_query_asset.
#
# What it demonstrates
#   • CubeQueryAssetComponent — Cube JSON query → pandas DataFrame asset
#   • ExternalCubeMetricAsset — declare Cube metrics in the Dagster catalog
#   • Cube as the "one metric definition per business concept" layer, with
#     Dagster orchestrating consumption
#
# Cost: $0. Docker-local Cube + built-in sample data. No cloud, no keys.
#
# Requirements
#   • uv (https://docs.astral.sh/uv/)
#   • docker (Docker Desktop or engine)
#
# Usage
#   ./setup_cube_query_demo.sh                          # → cube_demo/
#   ./setup_cube_query_demo.sh my_pipeline              # custom name

set -eo pipefail

PROJECT_NAME="${1:-cube_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"
CUBE_CONTAINER="cube_demo_server"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_YELLOW='\033[0;33m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; cleanup; exit 1; }

cleanup() {
  docker rm -f "$CUBE_CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup INT TERM

command -v uvx >/dev/null 2>&1 || fail "uvx not found. Install uv: https://docs.astral.sh/uv/"
command -v docker >/dev/null 2>&1 || fail "docker not found. Install Docker Desktop."
[ -d "$PROJECT_DIR" ] && fail "Directory already exists: $PROJECT_DIR"

info "Target project: $PROJECT_DIR"

# ── Start local Cube dev server ─────────────────────────────────────────────
info "Starting local Cube server via Docker (port 4000)…"
docker rm -f "$CUBE_CONTAINER" >/dev/null 2>&1 || true

# Prepare a minimal Cube model schema for the e-commerce sample data.
CUBE_WORK="$PROJECT_ABS/${PROJECT_NAME}_cube_conf"
rm -rf "$CUBE_WORK" && mkdir -p "$CUBE_WORK/model/cubes"

# Cube env — use in-process DuckDB with sample tables loaded into memory.
cat > "$CUBE_WORK/.env" <<'ENV'
CUBEJS_DEV_MODE=true
CUBEJS_DB_TYPE=duckdb
CUBEJS_DB_DUCKDB_DATABASE_PATH=:memory:
CUBEJS_API_SECRET=demo_secret_local_only
CUBEJS_SCHEDULED_REFRESH_TIMEZONES=UTC
ENV

# Cube schema — one cube (Orders) with count, totalAmount, plus time + status.
cat > "$CUBE_WORK/model/cubes/orders.yml" <<'YAML'
cubes:
  - name: Orders
    sql: >
      SELECT * FROM (VALUES
        (1, 100, 'completed', TIMESTAMP '2024-10-15 10:00:00'),
        (2, 100, 'completed', TIMESTAMP '2024-10-20 14:30:00'),
        (3, 200, 'completed', TIMESTAMP '2024-11-05 09:15:00'),
        (4, 150, 'processing', TIMESTAMP '2024-11-12 16:45:00'),
        (5, 300, 'completed', TIMESTAMP '2024-11-20 11:00:00'),
        (6,  75, 'shipped', TIMESTAMP '2024-11-25 13:30:00'),
        (7, 250, 'completed', TIMESTAMP '2024-12-01 08:20:00'),
        (8, 175, 'cancelled', TIMESTAMP '2024-12-05 15:00:00'),
        (9, 400, 'completed', TIMESTAMP '2024-12-10 12:45:00'),
        (10, 125, 'shipped', TIMESTAMP '2024-12-15 10:30:00'),
        (11, 220, 'completed', TIMESTAMP '2024-12-20 14:00:00'),
        (12,  95, 'processing', TIMESTAMP '2024-12-28 09:00:00')
      ) AS t(order_id, amount, status, created_at)

    measures:
      - name: count
        type: count

      - name: totalAmount
        sql: amount
        type: sum
        format: currency

      - name: avgAmount
        sql: amount
        type: avg
        format: currency

    dimensions:
      - name: status
        sql: status
        type: string

      - name: createdAt
        sql: created_at
        type: time
YAML

docker run -d --name "$CUBE_CONTAINER" \
  -p 4000:4000 \
  -v "$CUBE_WORK/model:/cube/conf/model" \
  --env-file "$CUBE_WORK/.env" \
  cubejs/cube:latest >/dev/null || fail "Docker run failed"

info "Waiting for Cube to accept connections…"
for i in $(seq 1 30); do
  if curl -sf http://localhost:4000/livez >/dev/null 2>&1; then
    ok "Cube server up on http://localhost:4000 (playground at /) after ${i}s"
    break
  fi
  sleep 1
  [ "$i" -eq 30 ] && fail "Cube didn't come up in 30s. Check: docker logs $CUBE_CONTAINER"
done

# ── Scaffold Dagster project ────────────────────────────────────────────────
info "Scaffolding Dagster project…"
uvx create-dagster project "$PROJECT_DIR" --uv-sync 2>&1 | tail -3 || fail "create-dagster failed"
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"

info "Installing deps…"
if [ -n "${DCC_SRC:-}" ] && [ -d "$DCC_SRC" ]; then
  info "Using local DCC source: $DCC_SRC"
  uv add --quiet "dagster-community-components @ ${DCC_SRC}" 'requests>=2.28' 'pandas>=1.5.0' 'tabulate>=0.9.0' || fail "uv add failed"
else
  uv add --quiet 'dagster-community-components @ git+https://github.com/eric-thomas-dagster/dagster-component-templates.git' 'requests>=2.28' 'pandas>=1.5.0' 'tabulate>=0.9.0' || fail "uv add failed"
fi
ok "Deps installed"

# ── Cube defs.yaml files ─────────────────────────────────────────────────────
mkdir -p "src/${PROJECT_NAME}/defs/cube_orders_count"
mkdir -p "src/${PROJECT_NAME}/defs/cube_orders_by_status"
mkdir -p "src/${PROJECT_NAME}/defs/external_orders_metric"

# 1. Simple query — total orders count + revenue.
cat > "src/${PROJECT_NAME}/defs/cube_orders_count/defs.yaml" <<YAML
type: dagster_community_components.CubeQueryAssetComponent
attributes:
  asset_name: cube_orders_summary
  api_url_env_var: CUBE_URL
  query:
    measures:
      - Orders.count
      - Orders.totalAmount
      - Orders.avgAmount
  group_name: cube
YAML

# 2. Grouped query — orders by status.
cat > "src/${PROJECT_NAME}/defs/cube_orders_by_status/defs.yaml" <<YAML
type: dagster_community_components.CubeQueryAssetComponent
attributes:
  asset_name: cube_orders_by_status
  api_url_env_var: CUBE_URL
  query:
    measures:
      - Orders.count
      - Orders.totalAmount
    dimensions:
      - Orders.status
    order:
      Orders.totalAmount: desc
  group_name: cube
YAML

# 3. External asset — declare-only, useful in catalogs.
cat > "src/${PROJECT_NAME}/defs/external_orders_metric/defs.yaml" <<YAML
type: dagster_community_components.ExternalCubeMetricAsset
attributes:
  asset_key: cube/orders/count
  cube_name: Orders
  measure_name: Orders.count
  metric_type: count
  cube_playground_url: http://localhost:4000/#/build
  group_name: cube
YAML

ok "Wrote 3 defs.yaml"

# ── Materialize ──────────────────────────────────────────────────────────────
export CUBE_URL="http://localhost:4000"
DM="${PROJECT_NAME}.definitions"
info "Materializing cube_orders_summary…"
CUBE_URL="$CUBE_URL" uv run dagster asset materialize --select cube_orders_summary -m "$DM" 2>&1 | tail -8 || fail "cube_orders_summary failed"

info "Materializing cube_orders_by_status…"
CUBE_URL="$CUBE_URL" uv run dagster asset materialize --select cube_orders_by_status -m "$DM" 2>&1 | tail -8 || fail "cube_orders_by_status failed"

echo
ok "Demo complete."
echo
cat <<EOF
Live services (still running):
  • Cube server: http://localhost:4000 (playground: http://localhost:4000/#/build)
  • Playground URL is clickable inside the Dagster asset catalog

Open the Dagster UI:
  cd $PROJECT_NAME
  CUBE_URL=$CUBE_URL uv run dg dev
    → three assets in the 'cube' group
    → each asset shows the Cube URL, measures, dimensions, row count,
      preview table, plus Cube's per-column annotation metadata

Explore Cube directly:
  open http://localhost:4000/#/build
    → drag measures + dimensions into a chart
    → see the same queries the components run

To stop:
  docker rm -f $CUBE_CONTAINER
EOF

trap - INT TERM
