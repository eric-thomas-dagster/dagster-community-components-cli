#!/usr/bin/env bash
# Oracle Database demo — Oracle Free in Docker (no license, no instant client).
#
# WHAT THIS DEMONSTRATES
#   The new oracle_resource component + generic SQL components (dataframe_to_table,
#   sql_command_job) against a local Oracle Database Free container. Same
#   components retarget at Oracle XE / Enterprise / Autonomous Database
#   by changing only host/port/service_name.
#
# Components exercised (3):
#   - oracle_resource          (shared connection — host/port/service_name/auth)
#   - synthetic_data_generator (upstream — 20 synthetic orders)
#   - dataframe_to_table       (DataFrame → Oracle table via SQLAlchemy)
#
# REQUIRES: Docker daemon running.
# COST: \$0 — Oracle Database Free is freely redistributable for any use.

set -euo pipefail
if ! docker info >/dev/null 2>&1; then echo "ERROR: Docker daemon not running."; exit 1; fi

PROJECT_DIR="${1:-oracle-demo}"
ORACLE_NAME=dg-oracle-demo
ORACLE_PORT=1521
ORACLE_PWD=DagsterDemo1
SERVICE_NAME=FREEPDB1

echo ">>> 1/5  Starting Oracle Database Free in Docker (takes 60-90s on first run)"
docker rm -f "$ORACLE_NAME" >/dev/null 2>&1 || true
sleep 1
docker run -d --name "$ORACLE_NAME" \
  -p $ORACLE_PORT:1521 \
  -e ORACLE_PWD="$ORACLE_PWD" \
  container-registry.oracle.com/database/free:latest >/dev/null

echo "    Waiting for Oracle to become ready..."
for i in $(seq 1 40); do
  if docker logs "$ORACLE_NAME" 2>&1 | grep -q "DATABASE IS READY TO USE"; then
    echo "    Oracle up after ${i}x5s."
    break
  fi
  sleep 5
done

echo ">>> 2/5  Verifying connectivity"
docker exec "$ORACLE_NAME" sh -c "echo 'SELECT 1 FROM dual;' | sqlplus -S system/$ORACLE_PWD@//localhost:1521/$SERVICE_NAME" 2>&1 | tail -5

echo ">>> 3/5  Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
mkdir -p out
PKG="$(ls src/ | head -1)"

uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q 'sqlalchemy>=2.0.0' 'oracledb>=2.0.0' pandas

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> 4/5  Installing 4 components"
# local_parquet_io_manager persists DataFrame outputs across subprocess boundaries
# (the default in-memory IO manager loses outputs between steps with multiprocess
# executor). Required for any multi-step asset chain.
for c in synthetic_data_generator oracle_resource dataframe_to_table local_parquet_io_manager; do
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
  base_dir: out/oracle-demo-storage
  create_dir: true"

write_yaml "oracle_resource" "type: $PKG.components.oracle_resource.component.OracleResourceComponent
attributes:
  resource_key: oracle_resource
  host: localhost
  port: $ORACLE_PORT
  service_name: $SERVICE_NAME
  username: system
  password: $ORACLE_PWD"

write_yaml "synthetic_data_generator" "type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: synthetic_orders
  schema_type: orders
  row_count: 20
  random_state: 42
  group_name: oracle_demo"

write_yaml "dataframe_to_table" "type: $PKG.components.dataframe_to_table.component.DataframeToTableComponent
attributes:
  asset_name: orders_in_oracle
  upstream_asset_key: synthetic_orders
  database_url: oracle+oracledb://system:$ORACLE_PWD@localhost:$ORACLE_PORT/?service_name=$SERVICE_NAME
  table_name: orders
  if_exists: replace
  group_name: oracle_demo"

cat <<MSG

>>> Setup complete.

    cd $PROJECT_DIR
    uv run dg check defs
    uv run dg launch --assets '*'

Verify rows landed in Oracle:
    docker exec $ORACLE_NAME sh -c "echo 'SELECT COUNT(*) FROM system.orders;' | sqlplus -S system/$ORACLE_PWD@//localhost:1521/$SERVICE_NAME"

Stop + clean up:
    docker rm -f $ORACLE_NAME

Production retargeting:
  - Oracle Autonomous DB: change host/port + service_name (1522 + ADB connect string).
  - Oracle Enterprise on-prem: change host + service_name (or sid).
  - Components otherwise unchanged.
MSG
