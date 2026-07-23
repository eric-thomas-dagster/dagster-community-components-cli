#!/usr/bin/env bash
# setup_doris_starrocks_demo.sh
#
# End-to-end round-trip against Apache Doris (and its StarRocks fork —
# both speak MySQL wire protocol, so the same components validate against
# either container with a one-line variable swap).
#
# What it validates
#   dataframe_to_doris          — bulk load DataFrame → Doris table
#   dataframe_to_starrocks      — same for StarRocks
#   doris_query_asset           — SQL query → DataFrame source
#   doris_resource              — shared connection
#   starrocks_resource          — shared connection
#   doris_workspace             — auto-enumerate tables as read-only assets
#   external_doris_table        — declare a Doris table as an external asset
#   external_starrocks_table    — same for StarRocks
#
# Cost: $0. Everything local via Docker.
#
# Requirements
#   • uv (https://docs.astral.sh/uv/)
#   • docker
#
# Usage
#   ./setup_doris_starrocks_demo.sh                              # → doris_demo/ w/ Doris
#   ./setup_doris_starrocks_demo.sh doris_demo doris             # explicit engine
#   ./setup_doris_starrocks_demo.sh sr_demo starrocks            # StarRocks variant

set -eo pipefail

PROJECT_NAME="${1:-doris_demo}"
ENGINE="${2:-doris}"    # doris | starrocks
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

command -v uvx    >/dev/null 2>&1 || fail "uvx not found. Install uv: https://docs.astral.sh/uv/"
command -v docker >/dev/null 2>&1 || fail "docker not found."
[ -d "$PROJECT_DIR" ] && fail "Directory already exists: $PROJECT_DIR"
[ "$ENGINE" = "doris" ] || [ "$ENGINE" = "starrocks" ] || fail "engine must be doris or starrocks"

# ── Docker image + ports per engine ─────────────────────────────────────────
if [ "$ENGINE" = "doris" ]; then
  IMAGE="apache/doris:doris-all-in-one-2.1.0"
  CONTAINER="dagster_doris_demo"
  QUERY_PORT=9030
  HTTP_PORT=8030
else
  IMAGE="starrocks/allin1-ubuntu:latest"
  CONTAINER="dagster_starrocks_demo"
  QUERY_PORT=9030
  HTTP_PORT=8030
fi

info "Engine: $ENGINE (image: $IMAGE)"

# ── Start the container ─────────────────────────────────────────────────────
if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
  info "Reusing existing container ${CONTAINER}"
  docker start "${CONTAINER}" >/dev/null
else
  info "Pulling + starting ${IMAGE} (first-run pull can be a few GB)…"
  docker run -d --name "${CONTAINER}" \
    -p ${QUERY_PORT}:${QUERY_PORT} \
    -p ${HTTP_PORT}:${HTTP_PORT} \
    "${IMAGE}" >/dev/null || fail "docker run failed"
fi

# ── Wait for readiness ──────────────────────────────────────────────────────
info "Waiting for ${ENGINE} to accept connections on :${QUERY_PORT} (up to 5 min)…"
for i in $(seq 1 60); do
  if docker exec "${CONTAINER}" bash -c "mysql -h127.0.0.1 -P${QUERY_PORT} -uroot -e 'SELECT 1' 2>/dev/null" >/dev/null 2>&1; then
    ok "${ENGINE} is up (took ~${i}× 5s)"
    break
  fi
  sleep 5
  [ "$i" = "60" ] && fail "timed out waiting for ${ENGINE}"
done

# ── Create the demo database + table + seed rows ────────────────────────────
info "Creating demo database + orders table + seed rows…"
docker exec "${CONTAINER}" bash -c "mysql -h127.0.0.1 -P${QUERY_PORT} -uroot <<'SQL'
CREATE DATABASE IF NOT EXISTS analytics;
USE analytics;
DROP TABLE IF EXISTS orders;
CREATE TABLE orders (
  order_id     INT,
  customer_id  INT,
  total        DECIMAL(10,2),
  order_date   DATE
) DUPLICATE KEY(order_id) DISTRIBUTED BY HASH(order_id) BUCKETS 1
  PROPERTIES('replication_num'='1');
INSERT INTO orders VALUES
  (1001, 1, 120.50, '2026-01-15'),
  (1002, 1, 85.00,  '2026-02-20'),
  (1003, 2, 45.00,  '2026-02-05'),
  (1004, 3, 300.00, '2026-03-22'),
  (1005, 3, 250.00, '2026-03-30');
SQL
" >/dev/null 2>&1 || fail "seed sql failed"
ok "Seeded 5 orders into analytics.orders"

# ── Env vars every component in the demo needs ──────────────────────────────
export ${ENGINE^^}_FE_HOST=127.0.0.1
export ${ENGINE^^}_USER=root
export ${ENGINE^^}_PASSWORD=""

# ── Scaffold Dagster project ────────────────────────────────────────────────
info "Scaffolding Dagster project…"
uvx create-dagster project "$PROJECT_DIR" --uv-sync 2>&1 | tail -3 || fail "create-dagster failed"
cd "$PROJECT_DIR"

info "Installing deps…"
if [ -n "${DCC_SRC:-}" ] && [ -d "$DCC_SRC" ]; then
  info "Using local DCC source: $DCC_SRC"
  uv add --quiet "dagster-community-components @ ${DCC_SRC}" pandas pymysql >/dev/null 2>&1
else
  uv add --quiet \
    "dagster-community-components @ git+https://github.com/eric-thomas-dagster/dagster-component-templates.git" \
    pandas pymysql >/dev/null 2>&1
fi
ok "Dependencies installed"

# ── defs.yaml files ─────────────────────────────────────────────────────────
PKG="${PROJECT_NAME}"
mkdir -p "src/${PKG}/defs/${ENGINE}"

# 1. Resource — shared connection
cat > "src/${PKG}/defs/${ENGINE}/resource.yaml" <<YAML
type: dagster_community_components.${ENGINE^}ResourceComponent
attributes:
  resource_key: ${ENGINE}_resource
  host_env_var: ${ENGINE^^}_FE_HOST
  query_port: ${QUERY_PORT}
  http_port: ${HTTP_PORT}
  database: analytics
  username_env_var: ${ENGINE^^}_USER
  password_env_var: ${ENGINE^^}_PASSWORD
YAML

# 2. Query asset — reads from the seeded orders table
cat > "src/${PKG}/defs/${ENGINE}/query.yaml" <<YAML
type: dagster_community_components.${ENGINE^}QueryAssetComponent
attributes:
  asset_name: recent_orders
  query: "SELECT order_id, customer_id, total, order_date FROM analytics.orders WHERE total > 100"
  host_env_var: ${ENGINE^^}_FE_HOST
  query_port: ${QUERY_PORT}
  database: analytics
  username_env_var: ${ENGINE^^}_USER
  password_env_var: ${ENGINE^^}_PASSWORD
  group_name: ${ENGINE}
YAML

# 3. External-asset declaration — the raw table as a first-class node
cat > "src/${PKG}/defs/${ENGINE}/external.yaml" <<YAML
type: dagster_community_components.External${ENGINE^}TableAsset
attributes:
  asset_key: ${ENGINE}/analytics/orders
  database: analytics
  table: orders
  fe_host: 127.0.0.1
  group_name: ${ENGINE}
YAML

# 4. Sink — write a DataFrame back into a new table (round-trip validation)
cat > "src/${PKG}/defs/${ENGINE}/upstream_dataframe.py" <<PY
"""Simple upstream DataFrame that the sink writes to ${ENGINE}."""
import dagster as dg
import pandas as pd


@dg.asset(group_name="${ENGINE}")
def orders_top3() -> pd.DataFrame:
    """3 top-value orders — round-trip test data for the sink."""
    return pd.DataFrame([
        {"order_id": 9001, "customer_id": 7, "total": 999.99, "order_date": "2026-07-01"},
        {"order_id": 9002, "customer_id": 8, "total": 850.00, "order_date": "2026-07-02"},
        {"order_id": 9003, "customer_id": 9, "total": 720.50, "order_date": "2026-07-03"},
    ])
PY

cat > "src/${PKG}/defs/${ENGINE}/sink.yaml" <<YAML
type: dagster_community_components.DataframeTo${ENGINE^}Component
attributes:
  asset_name: orders_top3_loaded
  upstream_asset_key: orders_top3
  table: orders
  database: analytics
  host_env_var: ${ENGINE^^}_FE_HOST
  http_port: ${HTTP_PORT}
  username_env_var: ${ENGINE^^}_USER
  password_env_var: ${ENGINE^^}_PASSWORD
  mode: append
  format: csv
  group_name: ${ENGINE}
YAML

# 5. Workspace — auto-enumerate all analytics.* tables as Dagster assets
cat > "src/${PKG}/defs/${ENGINE}/workspace.yaml" <<YAML
type: dagster_community_components.${ENGINE^}WorkspaceComponent
attributes:
  host_env_var: ${ENGINE^^}_FE_HOST
  query_port: ${QUERY_PORT}
  database: analytics
  username_env_var: ${ENGINE^^}_USER
  password_env_var: ${ENGINE^^}_PASSWORD
  schemas: [analytics]
  include_patterns: ["*"]
  import_tables: true
  import_views: true
  import_materialized_views: false
  group_name: ${ENGINE}_discovered
YAML

ok "Wrote 5 defs.yaml + 1 upstream .py"

# ── Validate ────────────────────────────────────────────────────────────────
info "Running dg check defs…"
uv run dg check defs 2>&1 | tail -8 || fail "dg check defs failed"
ok "Definitions validated"

# ── Final message ───────────────────────────────────────────────────────────
cat <<EOF

$(ok "Project scaffolded at: $PROJECT_DIR")

  Container: ${CONTAINER}  ($IMAGE)
  Env vars:  ${ENGINE^^}_FE_HOST, ${ENGINE^^}_USER, ${ENGINE^^}_PASSWORD

Next steps:

  cd ${PROJECT_NAME}
  export ${ENGINE^^}_FE_HOST=127.0.0.1
  export ${ENGINE^^}_USER=root
  export ${ENGINE^^}_PASSWORD=""
  uv run dg dev            # → http://localhost:3000

Materialize:
  • recent_orders          — SQL query source (5 seeded rows, filter total > 100 → 3 rows)
  • orders_top3            — synthetic DataFrame (3 rows)
  • orders_top3_loaded     — writes orders_top3 back into analytics.orders via bulk load
  • ${ENGINE}/analytics/orders — external-asset declaration (declare-only, no runtime)
  • workspace-discovered tables — one asset per table in analytics.*

Cleanup:
  docker stop ${CONTAINER} && docker rm ${CONTAINER}
EOF
