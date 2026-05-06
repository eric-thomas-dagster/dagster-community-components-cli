#!/usr/bin/env bash
# Azure Cache for Redis demo.
#
# WHAT THIS DEMONSTRATES
#   Synthetic e-commerce orders → redis_writer (HSET keyed by order_id) →
#   redis_reader (read all order:* hashes back as a DataFrame) →
#   dataframe_to_csv (high-value orders report).
#
#   Round-trip exercises both write and read components against Azure Cache
#   for Redis (TLS on port 6380), with read depending on write so lineage
#   flows through Redis.
#
# Pipeline (4 components):
#   synthetic_data_generator → redis_writer → redis_reader → dataframe_to_csv
#                                  │                 │
#                                  └─→ Azure Redis ──┘
#                                       (order:*)
#
# PREREQS
#   1. Azure subscription, az CLI signed in.
#   2. Microsoft.Cache provider registered:
#        az provider register --namespace Microsoft.Cache --wait
#   3. Azure Cache for Redis instance — see "Provisioning" below
#      (note: takes ~15-25 minutes to provision).
#
# REQUIRED ENV VARS
#   REDIS_HOST       <name>.redis.cache.windows.net
#   REDIS_PASSWORD   primary access key
#
# COST while running
#   Basic C0: ~$16/mo (continuous). Cannot auto-stop. Delete the instance
#   when done.
#
# TEARDOWN
#   az redis delete -g dagster-demo-rg -n <name> --yes

set -euo pipefail
PROJECT_DIR="${1:-azure-redis-demo}"

missing=()
[ -z "${REDIS_HOST:-}" ]     && missing+=("REDIS_HOST")
[ -z "${REDIS_PASSWORD:-}" ] && missing+=("REDIS_PASSWORD")

if [ ${#missing[@]} -gt 0 ]; then
  cat <<'NEED'
ERROR: missing env vars: see top of script.

To provision an Azure Cache for Redis instance + build the env vars:

    RG=dagster-demo-rg
    REDIS_NAME=dgredis$(openssl rand -hex 3)

    az group create -n "$RG" -l eastus 2>/dev/null || true
    az provider register --namespace Microsoft.Cache --wait

    # Basic C0 — smallest / cheapest tier (~$16/mo). Provisioning takes
    # 15-25 minutes; the create command blocks until ready.
    az redis create -g "$RG" -n "$REDIS_NAME" -l eastus \
        --sku Basic --vm-size c0

    export REDIS_HOST="$REDIS_NAME.redis.cache.windows.net"
    export REDIS_PASSWORD=$(az redis list-keys -g "$RG" -n "$REDIS_NAME" \
        --query primaryKey -o tsv)
NEED
  exit 1
fi

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas redis
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 4 community components"
$CLI add synthetic_data_generator --auto-install
$CLI add redis_writer            --auto-install
$CLI add redis_reader            --auto-install
$CLI add dataframe_to_csv        --auto-install

echo ">>> Writing demo defs.yaml"

cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: orders_raw
  schema_type: orders
  row_count: 30
  random_state: 42
  description: 30 synthetic e-commerce orders
  group_name: ingest
EOF

cat > "src/$PKG/defs/redis_writer/defs.yaml" <<EOF
type: $PKG.components.redis_writer.component.RedisWriterComponent
attributes:
  asset_name: orders_in_redis
  upstream_asset_key: orders_raw
  host_env_var: REDIS_HOST
  password_env_var: REDIS_PASSWORD
  port: 6380              # Azure Cache for Redis TLS port
  ssl: true               # required for Azure Cache for Redis
  key_column: order_id
  write_mode: hash        # HSET each row by order_id
  expire_seconds: 3600    # demo: keys expire after 1 hour
  group_name: cache
EOF

cat > "src/$PKG/defs/redis_reader/defs.yaml" <<EOF
type: $PKG.components.redis_reader.component.RedisReaderComponent
attributes:
  asset_name: orders_from_redis
  deps: [orders_in_redis]   # read after write
  host_env_var: REDIS_HOST
  password_env_var: REDIS_PASSWORD
  port: 6380
  ssl: true
  key_pattern: "ORD*"      # synthetic_data_generator order_ids start with ORD
  data_type: hash
  group_name: cache
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: orders_report
  upstream_asset_key: orders_from_redis
  file_path: /tmp/redis_orders_report.csv
  group_name: report
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Verify:
    head /tmp/redis_orders_report.csv
    redis-cli --tls -h "\$REDIS_HOST" -p 6380 -a "\$REDIS_PASSWORD" KEYS "ORD*" | head

Teardown:
    az redis delete -g dagster-demo-rg -n <name> --yes
MSG
