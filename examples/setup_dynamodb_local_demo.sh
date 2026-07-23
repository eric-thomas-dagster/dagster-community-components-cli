#!/usr/bin/env bash
# setup_dynamodb_local_demo.sh
#
# Docker-local end-to-end for the DynamoDB component set using AWS's
# `amazon/dynamodb-local` emulator. No real AWS account or credentials.
#
# Validates
#   dynamodb_resource   — shared client factory
#   dynamodb_reader     — scan a table → DataFrame
#   dynamodb_writer     — DataFrame → BatchWriteItem into a table
#
# Trick: boto3 respects the `AWS_ENDPOINT_URL_DYNAMODB` env var to point at
# a local endpoint. So no component change is needed — set the env var,
# use dummy AWS credentials, and every reader/writer talks to the emulator.
#
# Cost: $0. Requirements: uv, docker.

set -eo pipefail

PROJECT_NAME="${1:-dynamodb_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"
CONTAINER="dagster_dynamodb_demo"
IMAGE="amazon/dynamodb-local:latest"
HOST_PORT="${DYNAMODB_HOST_PORT:-8010}"   # host-side port (override with DYNAMODB_HOST_PORT=)

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

command -v uvx    >/dev/null 2>&1 || fail "uvx not found. Install uv: https://docs.astral.sh/uv/"
command -v docker >/dev/null 2>&1 || fail "docker not found."
[ -d "$PROJECT_DIR" ] && fail "Directory already exists: $PROJECT_DIR"

# ── Start dynamodb-local ────────────────────────────────────────────────────
if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
  info "Reusing existing container ${CONTAINER}"
  docker start "${CONTAINER}" >/dev/null
else
  info "Pulling + starting ${IMAGE}…"
  docker run -d --name "${CONTAINER}" \
    -p "${HOST_PORT}:8000" \
    "${IMAGE}" >/dev/null || fail "docker run failed (is port ${HOST_PORT} free? override with DYNAMODB_HOST_PORT=…)"
fi

info "Waiting for DynamoDB Local on :${HOST_PORT}…"
for i in $(seq 1 20); do
  # DynamoDB Local returns 400 with JSON on GET / — confirms it's the emulator, not something else on the port
  if curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${HOST_PORT}/" | grep -q "400"; then
    ok "DynamoDB Local is up"
    break
  fi
  sleep 1
  [ "$i" = "20" ] && fail "timed out waiting for DynamoDB Local on port ${HOST_PORT}"
done

# ── Env vars — dummy AWS creds + endpoint override + region ─────────────────
export AWS_ACCESS_KEY_ID="local"
export AWS_SECRET_ACCESS_KEY="local"
export AWS_REGION="us-east-1"
export AWS_ENDPOINT_URL_DYNAMODB="http://127.0.0.1:${HOST_PORT}"

# ── Create the demo table + seed 5 items ───────────────────────────────────
info "Creating 'orders' + 'orders_processed' tables + seeding 5 items…"
uvx --with boto3 python - <<'PY' >/dev/null 2>&1 || fail "seed script failed"
import os, boto3
ddb = boto3.client("dynamodb", region_name="us-east-1", endpoint_url=os.environ["AWS_ENDPOINT_URL_DYNAMODB"])
for name in ("orders", "orders_processed"):
    try:
        ddb.create_table(
            TableName=name,
            KeySchema=[{"AttributeName": "order_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "order_id", "AttributeType": "N"}],
            BillingMode="PAY_PER_REQUEST",
        )
    except ddb.exceptions.ResourceInUseException:
        pass
resource = boto3.resource("dynamodb", region_name="us-east-1", endpoint_url=os.environ["AWS_ENDPOINT_URL_DYNAMODB"])
table = resource.Table("orders")
for i in range(1, 6):
    status = "cancelled" if i == 3 else "active"
    table.put_item(Item={"order_id": i * 100, "customer_id": i, "total": str(100 * i + 0.5), "status": status, "created_at": f"2026-07-2{i}"})
PY
ok "Seeded 5 items (4 active + 1 cancelled)"

# ── Scaffold Dagster project ────────────────────────────────────────────────
info "Scaffolding Dagster project…"
uvx create-dagster project "$PROJECT_DIR" --uv-sync 2>&1 | tail -3 || fail "create-dagster failed"
cd "$PROJECT_DIR"

info "Installing deps…"
if [ -n "${DCC_SRC:-}" ] && [ -d "$DCC_SRC" ]; then
  uv add --quiet "dagster-community-components @ ${DCC_SRC}" pandas boto3 >/dev/null 2>&1
else
  uv add --quiet \
    "dagster-community-components @ git+https://github.com/eric-thomas-dagster/dagster-component-templates.git" \
    pandas boto3 >/dev/null 2>&1
fi
ok "Dependencies installed"

# ── defs.yaml files — one dir per component (autoloader convention) ─────────
PKG="${PROJECT_NAME}"
mkdir -p "src/${PKG}/defs/dynamodb_resource" \
         "src/${PKG}/defs/dynamodb_reader" \
         "src/${PKG}/defs/dynamodb_writer"

cat > "src/${PKG}/defs/dynamodb_resource/defs.yaml" <<'YAML'
type: dagster_community_components.DynamoDBResourceComponent
attributes:
  resource_key: dynamodb_resource
  region_name: us-east-1
  aws_access_key_id_env_var: AWS_ACCESS_KEY_ID
  aws_secret_access_key_env_var: AWS_SECRET_ACCESS_KEY
YAML

cat > "src/${PKG}/defs/dynamodb_reader/defs.yaml" <<'YAML'
type: dagster_community_components.DynamodbReaderComponent
attributes:
  asset_name: active_orders
  table_name: orders
  aws_region: us-east-1
  limit: 500
  group_name: dynamodb_sources
YAML

cat > "src/${PKG}/defs/dynamodb_writer/defs.yaml" <<'YAML'
type: dagster_community_components.DynamodbWriterComponent
attributes:
  asset_name: active_orders_written
  upstream_asset_key: active_orders
  table: orders_processed
  aws_region: us-east-1
  group_name: dynamodb_sinks
YAML

ok "Wrote 3 defs.yaml (100% components — no custom Python)"

info "Running dg check defs…"
uv run dg check defs 2>&1 | tail -6 || fail "dg check defs failed"
ok "Definitions validated"

cat <<EOF

$(ok "Project scaffolded at: $PROJECT_DIR")

  Container: ${CONTAINER}  (${IMAGE})
  Local endpoint: http://127.0.0.1:${HOST_PORT}

Next steps:
  cd ${PROJECT_NAME}
  # boto3 respects AWS_ENDPOINT_URL_DYNAMODB to point at the emulator
  export AWS_ACCESS_KEY_ID=local
  export AWS_SECRET_ACCESS_KEY=local
  export AWS_REGION=us-east-1
  export AWS_ENDPOINT_URL_DYNAMODB=http://127.0.0.1:${HOST_PORT}
  uv run dg dev

Materialize (in order):
  active_orders          — Scan → 5 rows (all seeded items)
  active_orders_written  — BatchWriteItem into orders_processed table

Cleanup:
  docker stop ${CONTAINER} && docker rm ${CONTAINER}
EOF
