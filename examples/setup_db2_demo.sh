#!/usr/bin/env bash
# IBM Db2 Community Edition demo — free Db2 in Docker.
#
# WHAT THIS DEMONSTRATES
#   The new db2_resource component + generic SQL components against a local
#   Db2 Community Edition container. Same components retarget at Db2 on
#   Cloud / Db2 Warehouse by changing only host/port/ssl.
#
# Components exercised (4):
#   - db2_resource             (shared connection)
#   - synthetic_data_generator (upstream — 20 synthetic orders)
#   - dataframe_to_table       (DataFrame → Db2 table via SQLAlchemy / ibm_db_sa)
#   - local_parquet_io_manager (persistent IO manager — required across subprocesses)
#
# REQUIRES: Docker daemon. The Db2 image is ~3GB; first pull takes a few minutes.
# COST: \$0 — Db2 Community Edition is free for non-production use.
#
# Note: Db2 Community runs as a privileged container — required by the Db2
# product (it sets kernel parameters at startup).

set -euo pipefail
if ! docker info >/dev/null 2>&1; then echo "ERROR: Docker daemon not running."; exit 1; fi

PROJECT_DIR="${1:-db2-demo}"
DB2_NAME=dg-db2-demo
DB2_PORT=50000
DB2_DB=testdb
DB2_USER=db2inst1
DB2_PASS=DagsterDemo1

echo ">>> 1/5  Starting Db2 Community Edition in Docker (takes 60-120s on first run)"
docker rm -f "$DB2_NAME" >/dev/null 2>&1 || true
sleep 1
docker run -d --name "$DB2_NAME" --platform linux/amd64 --privileged \
  -p $DB2_PORT:50000 \
  -e LICENSE=accept \
  -e DB2INST1_PASSWORD="$DB2_PASS" \
  -e DBNAME="$DB2_DB" \
  icr.io/db2_community/db2:latest >/dev/null

echo "    Waiting for Db2 to finish initializing (Db2 boot is slow)..."
for i in $(seq 1 40); do
  if docker logs "$DB2_NAME" 2>&1 | grep -q "Setup has completed"; then
    echo "    Db2 ready after ${i}x10s."
    break
  fi
  sleep 10
done

# A small extra wait for the SQL listener to come up
sleep 5

echo ">>> 2/5  Verifying Db2 connectivity"
docker exec "$DB2_NAME" su - "$DB2_USER" -c "db2 connect to $DB2_DB; db2 'VALUES CURRENT TIMESTAMP'" 2>&1 | tail -5

echo ">>> 3/5  Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q 'sqlalchemy>=2.0.0' 'ibm_db>=3.2.0' 'ibm_db_sa>=0.4.0' pandas

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> 4/5  Installing 4 components"
for c in synthetic_data_generator db2_resource dataframe_to_table local_parquet_io_manager; do
  $CLI add $c --auto-install
done

echo ">>> 5/5  Writing defs.yaml"

write_yaml() {
  local name="$1"; shift
  local body="$1"; shift
  mkdir -p "src/$PKG/defs/$name"
  printf "%s\n" "$body" > "src/$PKG/defs/$name/defs.yaml"
}

write_yaml "local_parquet_io_manager" "type: $PKG.components.local_parquet_io_manager.component.LocalParquetIOManagerComponent
attributes:
  resource_key: io_manager
  base_dir: /tmp/db2-demo-storage
  create_dir: true"

write_yaml "db2_resource" "type: $PKG.components.db2_resource.component.Db2ResourceComponent
attributes:
  resource_key: db2_resource
  host: localhost
  port: $DB2_PORT
  database: $DB2_DB
  username: $DB2_USER
  password_env_var: DB2_PASSWORD"

write_yaml "synthetic_data_generator" "type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: synthetic_orders
  schema_type: orders
  row_count: 20
  random_state: 42
  group_name: db2_demo"

write_yaml "dataframe_to_table" "type: $PKG.components.dataframe_to_table.component.DataframeToTableComponent
attributes:
  asset_name: orders_in_db2
  upstream_asset_key: synthetic_orders
  database_url_env_var: DB2_URL
  table_name: ORDERS
  if_exists: replace
  group_name: db2_demo"

cat > .env.demo <<EOF
export DB2_PASSWORD='$DB2_PASS'
export DB2_URL='db2+ibm_db://$DB2_USER:$DB2_PASS@localhost:$DB2_PORT/$DB2_DB'
EOF

cat <<MSG

>>> Setup complete.

    cd $PROJECT_DIR
    source .env.demo
    uv run dg check defs
    uv run dg launch --assets '*'

Verify rows in Db2:
    docker exec $DB2_NAME su - $DB2_USER -c "db2 connect to $DB2_DB; db2 'SELECT COUNT(*) FROM $DB2_USER.ORDERS'"

Stop + clean up:
    docker rm -f $DB2_NAME

Production retargeting:
  - Db2 on Cloud: change host + port (31xxx) + ssl: true.
  - Db2 Warehouse: identical shape, just different host.
MSG
