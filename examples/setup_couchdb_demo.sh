#!/usr/bin/env bash
# setup_couchdb_demo.sh
#
# Docker-local end-to-end for the CouchDB document-DB component set.
#
# Validates
#   couchdb_resource      — shared connection handle
#   couchdb_reader        — Mango-selector query → DataFrame
#   couchdb_writer        — DataFrame → upsert docs into a database
#
# Round-trip: setup script seeds 5 docs → reader queries active ones →
# Python asset transforms them → writer upserts back to a target database.
#
# Cost: $0. Requirements: uv, docker.

set -eo pipefail

PROJECT_NAME="${1:-couchdb_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"
CONTAINER="dagster_couchdb_demo"
IMAGE="couchdb:3"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

command -v uvx    >/dev/null 2>&1 || fail "uvx not found. Install uv: https://docs.astral.sh/uv/"
command -v docker >/dev/null 2>&1 || fail "docker not found."
[ -d "$PROJECT_DIR" ] && fail "Directory already exists: $PROJECT_DIR"

# ── Start CouchDB ───────────────────────────────────────────────────────────
if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
  info "Reusing existing container ${CONTAINER}"
  docker start "${CONTAINER}" >/dev/null
else
  info "Pulling + starting ${IMAGE}…"
  docker run -d --name "${CONTAINER}" \
    -e COUCHDB_USER=admin \
    -e COUCHDB_PASSWORD=admin \
    -p 5984:5984 \
    "${IMAGE}" >/dev/null || fail "docker run failed"
fi

# ── Wait for ready ──────────────────────────────────────────────────────────
info "Waiting for CouchDB on :5984…"
for i in $(seq 1 30); do
  if curl -s -f http://admin:admin@127.0.0.1:5984/ >/dev/null 2>&1; then
    ok "CouchDB is up"
    break
  fi
  sleep 2
  [ "$i" = "30" ] && fail "timed out waiting for CouchDB"
done

# ── Create the demo database + seed docs (idempotent — drops + recreates) ───
info "Creating 'orders' + 'orders_processed' databases + seeding 5 docs…"
curl -s -X DELETE http://admin:admin@127.0.0.1:5984/orders >/dev/null
curl -s -X DELETE http://admin:admin@127.0.0.1:5984/orders_processed >/dev/null
curl -s -X PUT http://admin:admin@127.0.0.1:5984/orders >/dev/null
curl -s -X PUT http://admin:admin@127.0.0.1:5984/orders_processed >/dev/null
for i in 1 2 3 4 5; do
  status="active"
  [ "$i" = "3" ] && status="cancelled"
  curl -s -X POST http://admin:admin@127.0.0.1:5984/orders \
    -H "Content-Type: application/json" \
    -d "{\"order_id\": ${i}00, \"customer_id\": ${i}, \"total\": $((100 * i)).50, \"status\": \"${status}\", \"created_at\": \"2026-07-2${i}\"}" >/dev/null
done
ok "Seeded 5 docs (4 active + 1 cancelled)"

# ── Env vars ────────────────────────────────────────────────────────────────
export COUCHDB_URL="http://admin:admin@127.0.0.1:5984"
export COUCHDB_PASSWORD="admin"

# ── Scaffold Dagster project ────────────────────────────────────────────────
info "Scaffolding Dagster project…"
uvx create-dagster project "$PROJECT_DIR" --uv-sync 2>&1 | tail -3 || fail "create-dagster failed"
cd "$PROJECT_DIR"

info "Installing deps…"
if [ -n "${DCC_SRC:-}" ] && [ -d "$DCC_SRC" ]; then
  uv add --quiet "dagster-community-components @ ${DCC_SRC}" pandas requests couchdb >/dev/null 2>&1
else
  uv add --quiet \
    "dagster-community-components @ git+https://github.com/eric-thomas-dagster/dagster-component-templates.git" \
    pandas requests couchdb >/dev/null 2>&1
fi
ok "Dependencies installed"

# ── defs.yaml files — one dir per component (autoloader convention) ─────────
PKG="${PROJECT_NAME}"
mkdir -p "src/${PKG}/defs/couchdb_resource" \
         "src/${PKG}/defs/couchdb_reader" \
         "src/${PKG}/defs/couchdb_writer"

# 1. Shared resource
cat > "src/${PKG}/defs/couchdb_resource/defs.yaml" <<'YAML'
type: dagster_community_components.CouchDBResourceComponent
attributes:
  resource_key: couchdb_resource
  url: "http://admin:admin@127.0.0.1:5984"
  username: admin
  password_env_var: COUCHDB_PASSWORD
YAML

# 2. Reader — active orders via Mango selector
cat > "src/${PKG}/defs/couchdb_reader/defs.yaml" <<'YAML'
type: dagster_community_components.CouchdbReaderComponent
attributes:
  asset_name: active_orders
  url_env_var: COUCHDB_URL
  database: orders
  selector:
    status: active
  fields:
    - order_id
    - customer_id
    - total
    - status
    - created_at
  limit: 500
  group_name: couchdb_sources
YAML

# 3. Writer — upserts the active docs back into a target database
cat > "src/${PKG}/defs/couchdb_writer/defs.yaml" <<'YAML'
type: dagster_community_components.CouchdbWriterComponent
attributes:
  asset_name: active_orders_upserted
  upstream_asset_key: active_orders
  url_env_var: COUCHDB_URL
  database: orders_processed
  if_exists: upsert
  id_column: order_id
  group_name: couchdb_sinks
YAML

ok "Wrote 3 defs.yaml (100% components — no custom Python)"

# ── Validate ────────────────────────────────────────────────────────────────
info "Running dg check defs…"
uv run dg check defs 2>&1 | tail -6 || fail "dg check defs failed"
ok "Definitions validated"

cat <<EOF

$(ok "Project scaffolded at: $PROJECT_DIR")

  Container: ${CONTAINER}  (${IMAGE})
  CouchDB URL: http://admin:admin@127.0.0.1:5984

Next steps:
  cd ${PROJECT_NAME}
  export COUCHDB_URL="http://admin:admin@127.0.0.1:5984"
  export COUCHDB_PASSWORD="admin"
  uv run dg dev            # → http://localhost:3000

Materialize (in order):
  active_orders            — Mango selector returns 4 active orders
  active_orders_upserted   — CouchdbWriter upserts them into orders_processed db

Cleanup:
  docker stop ${CONTAINER} && docker rm ${CONTAINER}
EOF
