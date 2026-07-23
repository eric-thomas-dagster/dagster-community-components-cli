#!/usr/bin/env bash
# setup_mariadb_demo.sh
#
# Docker-local end-to-end for MariaDB — using the MySQL components unchanged.
# MariaDB is wire-compatible with MySQL, so `mysql_resource` +
# `mysql_io_manager` + `dataframe_to_table` all Just Work against a
# mariadb:11 container with zero component changes.
#
# Validates:
#   MySQLResourceComponent    (against MariaDB)
#   dataframe_to_table sink   (against MariaDB)
#
# Cost: $0. Requirements: uv, docker.

set -eo pipefail

PROJECT_NAME="${1:-mariadb_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"
CONTAINER="dagster_mariadb_demo"
IMAGE="mariadb:11"
DB_PORT="${MARIADB_HOST_PORT:-13306}"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

command -v uvx    >/dev/null 2>&1 || fail "uvx not found. Install uv: https://docs.astral.sh/uv/"
command -v docker >/dev/null 2>&1 || fail "docker not found."
[ -d "$PROJECT_DIR" ] && fail "Directory already exists: $PROJECT_DIR"

# ── Start MariaDB ───────────────────────────────────────────────────────────
if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
  info "Reusing existing container ${CONTAINER}"
  docker start "${CONTAINER}" >/dev/null
else
  info "Pulling + starting ${IMAGE}…"
  docker run -d --name "${CONTAINER}" \
    -e MARIADB_ROOT_PASSWORD=demo \
    -e MARIADB_DATABASE=analytics \
    -e MARIADB_USER=dagster \
    -e MARIADB_PASSWORD=demo \
    -p "${DB_PORT}:3306" \
    "${IMAGE}" >/dev/null || fail "docker run failed"
fi

info "Waiting for MariaDB on :${DB_PORT} (up to 60s)…"
for i in $(seq 1 60); do
  # Use the app user (created via MARIADB_USER env var) — TCP protocol +
  # skip-ssl so we bypass unix_socket auth + client-side TLS quirks.
  if docker exec "${CONTAINER}" mariadb --protocol=tcp -h127.0.0.1 -udagster -pdemo --skip-ssl analytics -e "SELECT 1" >/dev/null 2>&1; then
    ok "MariaDB is up (${i}s)"
    break
  fi
  sleep 1
  [ "$i" = "60" ] && fail "timed out waiting for MariaDB"
done

# ── Seed analytics.orders ───────────────────────────────────────────────────
info "Creating analytics.orders + seeding 5 rows…"
docker exec -i "${CONTAINER}" mariadb --protocol=tcp -h127.0.0.1 -udagster -pdemo --skip-ssl analytics <<'SQL' >/dev/null
DROP TABLE IF EXISTS orders;
CREATE TABLE orders (
  order_id     INT PRIMARY KEY,
  customer_id  INT,
  total        DECIMAL(10, 2),
  status       VARCHAR(20),
  created_at   DATETIME
) ENGINE=InnoDB;
INSERT INTO orders VALUES
  (100, 1, 100.50, 'active', '2026-07-21 10:00'),
  (200, 2, 200.50, 'active', '2026-07-22 10:00'),
  (300, 3, 300.50, 'cancelled', '2026-07-23 10:00'),
  (400, 4, 400.50, 'active', '2026-07-24 10:00'),
  (500, 5, 500.50, 'active', '2026-07-25 10:00');
SQL
ok "Seeded 5 rows (4 active + 1 cancelled)"

# ── Env vars ────────────────────────────────────────────────────────────────
export MARIADB_PASSWORD="demo"

# ── Scaffold Dagster project ────────────────────────────────────────────────
info "Scaffolding Dagster project…"
uvx create-dagster project "$PROJECT_DIR" --uv-sync 2>&1 | tail -3 || fail "create-dagster failed"
cd "$PROJECT_DIR"

info "Installing deps…"
if [ -n "${DCC_SRC:-}" ] && [ -d "$DCC_SRC" ]; then
  uv add --quiet "dagster-community-components @ ${DCC_SRC}" \
    pandas mysql-connector-python sqlalchemy pymysql >/dev/null 2>&1
else
  uv add --quiet \
    "dagster-community-components @ git+https://github.com/eric-thomas-dagster/dagster-component-templates.git" \
    pandas mysql-connector-python sqlalchemy pymysql >/dev/null 2>&1
fi
ok "Dependencies installed"

# ── defs.yaml — one dir per component (autoloader convention) ───────────────
PKG="${PROJECT_NAME}"
mkdir -p "src/${PKG}/defs/mariadb_resource" \
         "src/${PKG}/defs/synthetic_orders" \
         "src/${PKG}/defs/orders_snapshot"

# MySQL resource works against MariaDB with no changes
cat > "src/${PKG}/defs/mariadb_resource/defs.yaml" <<YAML
type: dagster_community_components.MySQLResourceComponent
attributes:
  resource_key: mariadb_resource
  host: 127.0.0.1
  port: ${DB_PORT}
  database: analytics
  username: dagster
  password_env_var: MARIADB_PASSWORD
  ssl_disabled: true
YAML

# Synthetic upstream that produces a small DataFrame
cat > "src/${PKG}/defs/synthetic_orders/defs.yaml" <<YAML
type: dagster_community_components.SyntheticDataGeneratorComponent
attributes:
  asset_name: synthetic_orders
  schema_type: orders
  row_count: 100
  group_name: mariadb
YAML

# dataframe_to_table sink — writes back to MariaDB via SQLAlchemy
cat > "src/${PKG}/defs/orders_snapshot/defs.yaml" <<YAML
type: dagster_community_components.DataframeToTableComponent
attributes:
  asset_name: orders_snapshot
  upstream_asset_key: synthetic_orders
  database_url_env_var: MARIADB_URL
  table: orders_snapshot
  if_exists: replace
  group_name: mariadb
YAML

ok "Wrote 3 defs.yaml (100% components — no custom Python)"

info "Running dg check defs…"
export MARIADB_URL="mysql+pymysql://dagster:demo@127.0.0.1:${DB_PORT}/analytics"
uv run dg check defs 2>&1 | tail -6 || fail "dg check defs failed"
ok "Definitions validated"

cat <<EOF

$(ok "Project scaffolded at: $PROJECT_DIR")

  Container: ${CONTAINER}  (${IMAGE})
  MariaDB port: ${DB_PORT}
  Database: analytics  User: dagster  Password: demo

Next steps:
  cd ${PROJECT_NAME}
  export MARIADB_PASSWORD=demo
  export MARIADB_URL="mysql+pymysql://dagster:demo@127.0.0.1:${DB_PORT}/analytics"
  uv run dg dev            # → http://localhost:3000

Materialize (in order):
  synthetic_orders    — Generator: 100 synthetic orders as DataFrame
  orders_snapshot     — dataframe_to_table sink: writes back to analytics.orders_snapshot

Verify:
  docker exec ${CONTAINER} mariadb --protocol=tcp -h127.0.0.1 -udagster -pdemo --skip-ssl analytics \\
    -e "SELECT COUNT(*) FROM orders_snapshot;"

Cleanup:
  docker rm -f ${CONTAINER}
EOF
