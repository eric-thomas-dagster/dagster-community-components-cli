#!/usr/bin/env bash
# setup_clickhouse_advanced_demo.sh
#
# Docker-local end-to-end for the ClickHouse code-level components:
#   ClickhouseIOManagerComponent               — DataFrame → table via dagster-clickhouse-pandas
#   ClickHouseTableObservationSensorComponent  — row_count / bytes / active_parts sensor
#
# Also exercises (already `live`):
#   ClickHouseResourceComponent
#   ExternalClickHouseTableComponent
#
# Companion demo to `clickhouse.md` — that one validates the reader/writer/resource
# round-trip; this one validates the IO manager + observation sensor.
#
# Uses HTTP port 18123 so it can share the `clickhouse-demo-server` container
# with the base demo (delete + recreate if you want a clean state).
#
# Cost: $0. Requirements: uv, docker.

set -eo pipefail

PROJECT_NAME="${1:-clickhouse_advanced_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"
CH_CONTAINER="${CH_CONTAINER:-clickhouse-demo-server}"
CH_PORT_HTTP="${CH_PORT_HTTP:-18123}"
CH_PORT_NATIVE="${CH_PORT_NATIVE:-19000}"
IMAGE="clickhouse/clickhouse-server:latest"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

command -v uvx    >/dev/null 2>&1 || fail "uvx not found. Install uv: https://docs.astral.sh/uv/"
command -v docker >/dev/null 2>&1 || fail "docker not found."
[ -d "$PROJECT_DIR" ] && fail "Directory already exists: $PROJECT_DIR"

# ── Start ClickHouse ────────────────────────────────────────────────────────
if docker ps -a --format '{{.Names}}' | grep -q "^${CH_CONTAINER}$"; then
  info "Reusing existing container ${CH_CONTAINER}"
  docker start "${CH_CONTAINER}" >/dev/null
else
  info "Pulling + starting ${IMAGE}…"
  docker run -d --name "${CH_CONTAINER}" \
    -p "${CH_PORT_HTTP}:8123" -p "${CH_PORT_NATIVE}:9000" \
    -e CLICKHOUSE_USER=default \
    -e CLICKHOUSE_PASSWORD= \
    -e CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1 \
    --ulimit nofile=262144:262144 \
    "${IMAGE}" >/dev/null || fail "docker run failed"
fi

info "Waiting for ClickHouse on :${CH_PORT_HTTP} (up to 90s)…"
for i in $(seq 1 90); do
  if curl -sS "http://localhost:${CH_PORT_HTTP}/ping" 2>/dev/null | grep -q "Ok"; then
    ok "ClickHouse is up (${i}s)"
    break
  fi
  sleep 1
  [ "$i" = "90" ] && fail "timed out waiting for ClickHouse"
done

# ── Seed analytics.orders so the observation sensor has something to watch ──
info "Creating analytics.orders + seeding 100 rows (idempotent — drops + recreates)…"
curl -sS -X POST "http://localhost:${CH_PORT_HTTP}/" --data "CREATE DATABASE IF NOT EXISTS analytics" >/dev/null
curl -sS -X POST "http://localhost:${CH_PORT_HTTP}/" --data "DROP TABLE IF EXISTS analytics.orders" >/dev/null
curl -sS -X POST "http://localhost:${CH_PORT_HTTP}/" --data "$(cat <<SQL
CREATE TABLE IF NOT EXISTS analytics.orders (
  order_id     UInt64,
  customer_id  UInt64,
  total        Float64,
  status       String,
  created_at   DateTime
) ENGINE = MergeTree()
ORDER BY (created_at, order_id)
SQL
)" >/dev/null
curl -sS -X POST "http://localhost:${CH_PORT_HTTP}/" --data "$(cat <<SQL
INSERT INTO analytics.orders
SELECT
  number * 100 AS order_id,
  (number % 10) + 1 AS customer_id,
  round(rand() % 1000 + 50 + 0.5, 2) AS total,
  if(number % 20 = 0, 'cancelled', 'active') AS status,
  now() - INTERVAL number MINUTE AS created_at
FROM numbers(100)
SQL
)" >/dev/null
ok "Seeded 100 rows (95 active + 5 cancelled)"

# ── Env vars ────────────────────────────────────────────────────────────────
export CLICKHOUSE_HOST="localhost"
export CLICKHOUSE_USER="default"
export CLICKHOUSE_PASSWORD=""

# ── Scaffold Dagster project ────────────────────────────────────────────────
info "Scaffolding Dagster project…"
uvx create-dagster project "$PROJECT_DIR" --uv-sync 2>&1 | tail -3 || fail "create-dagster failed"
cd "$PROJECT_DIR"

info "Installing deps…"
if [ -n "${DCC_SRC:-}" ] && [ -d "$DCC_SRC" ]; then
  uv add --quiet "dagster-community-components @ ${DCC_SRC}" \
    pandas clickhouse-connect dagster-clickhouse-pandas >/dev/null 2>&1
else
  uv add --quiet \
    "dagster-community-components @ git+https://github.com/eric-thomas-dagster/dagster-component-templates.git" \
    pandas clickhouse-connect dagster-clickhouse-pandas >/dev/null 2>&1
fi
ok "Dependencies installed"

# ── defs.yaml — one dir per component (autoloader convention) ───────────────
PKG="${PROJECT_NAME}"
mkdir -p "src/${PKG}/defs/clickhouse_resource" \
         "src/${PKG}/defs/clickhouse_io_manager" \
         "src/${PKG}/defs/external_clickhouse_table" \
         "src/${PKG}/defs/clickhouse_observation_sensor"

cat > "src/${PKG}/defs/clickhouse_resource/defs.yaml" <<YAML
type: dagster_community_components.ClickHouseResourceComponent
attributes:
  resource_key: clickhouse_resource
  host: localhost
  port: ${CH_PORT_HTTP}
  database: analytics
  username: default
  password: ""
  secure: false
YAML

# IO manager — every DataFrame asset auto-persists as a ClickHouse table
cat > "src/${PKG}/defs/clickhouse_io_manager/defs.yaml" <<YAML
type: dagster_community_components.ClickhouseIOManagerComponent
attributes:
  resource_key: io_manager
  host: localhost
  port: ${CH_PORT_HTTP}
  database: analytics
  username_env_var: CLICKHOUSE_USER
  password_env_var: CLICKHOUSE_PASSWORD
  secure: false
YAML

# Declare the seeded table as a first-class Dagster asset
cat > "src/${PKG}/defs/external_clickhouse_table/defs.yaml" <<YAML
type: dagster_community_components.ExternalClickHouseTableComponent
attributes:
  asset_key: clickhouse/analytics/orders
  database: analytics
  table: orders
  host_env_var: CLICKHOUSE_HOST
  port: ${CH_PORT_HTTP}
  username_env_var: CLICKHOUSE_USER
  password_env_var: CLICKHOUSE_PASSWORD
  group_name: clickhouse
  description: |
    Seed table populated by the setup script; observed by the sensor below
    and auto-registered here as a Dagster asset.
YAML

# Sensor — periodic row_count / bytes / parts observation
cat > "src/${PKG}/defs/clickhouse_observation_sensor/defs.yaml" <<YAML
type: dagster_community_components.ClickHouseTableObservationSensorComponent
attributes:
  sensor_name: clickhouse_orders_observation
  asset_key: clickhouse/analytics/orders
  database: analytics
  table: orders
  host_env_var: CLICKHOUSE_HOST
  port: ${CH_PORT_HTTP}
  username_env_var: CLICKHOUSE_USER
  password_env_var: CLICKHOUSE_PASSWORD
  check_interval_seconds: 60
  default_status: stopped
YAML

ok "Wrote 4 defs.yaml (100% components — no custom Python)"

info "Running dg check defs…"
uv run dg check defs 2>&1 | tail -6 || fail "dg check defs failed"
ok "Definitions validated"

cat <<EOF

$(ok "Project scaffolded at: $PROJECT_DIR")

  Container: ${CH_CONTAINER}  (${IMAGE})
  ClickHouse HTTP: http://localhost:${CH_PORT_HTTP}

Next steps:
  cd ${PROJECT_NAME}
  export CLICKHOUSE_HOST=localhost
  export CLICKHOUSE_USER=default
  export CLICKHOUSE_PASSWORD=
  uv run dg dev            # → http://localhost:3000

What to click through in the UI:
  1. clickhouse/analytics/orders  — external asset (declared, no runtime).
  2. clickhouse_orders_observation — turn the sensor ON; it will emit
     AssetObservation events every 60s carrying:
       - row_count, disk_bytes, active_parts, engine
     Visible under the asset's Overview tab.
  3. If you want to test the IO manager end-to-end, add a Python asset that
     returns a DataFrame — because the project's io_manager is the ClickHouse
     one, that asset will auto-materialize as an analytics.<asset_name> table.

Cleanup:
  docker rm -f ${CH_CONTAINER}
EOF
