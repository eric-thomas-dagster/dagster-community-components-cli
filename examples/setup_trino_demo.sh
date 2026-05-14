#!/usr/bin/env bash
# Trino demo — single-container Trino coordinator with the built-in `memory`
# catalog. No SaaS, no auth.

set -euo pipefail
if ! docker info >/dev/null 2>&1; then echo "ERROR: Docker daemon not running."; exit 1; fi

PROJECT_DIR="${1:-trino-demo}"
TRINO_NAME=dg-trino-demo
TRINO_PORT=8089

echo ">>> 1/4  Starting Trino in Docker on :$TRINO_PORT"
docker rm -f "$TRINO_NAME" >/dev/null 2>&1 || true
sleep 1
docker run -d --name "$TRINO_NAME" -p $TRINO_PORT:8080 trinodb/trino:latest >/dev/null

echo "    Waiting for Trino to become ready + finish initialization (45-90s on first run)..."
for i in $(seq 1 60); do
  if docker exec "$TRINO_NAME" trino --server localhost:8080 --execute "SHOW CATALOGS" >/dev/null 2>&1; then
    echo "    Trino fully initialized."
    break
  fi
  sleep 3
done

echo ">>> 2/4  Creating schema in memory catalog"
docker exec "$TRINO_NAME" trino --server localhost:8080 --execute "CREATE SCHEMA IF NOT EXISTS memory.demo" >/dev/null

echo ">>> 3/4  Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q 'trino>=0.320.0' pandas

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> 4/4  Installing 3 components + writing defs.yaml"
for c in synthetic_data_generator trino_resource trino_io_manager; do
  $CLI add $c --auto-install
done

write_yaml() {
  local name="$1"; shift
  local body="$1"; shift
  mkdir -p "src/$PKG/defs/$name"
  printf "%s\n" "$body" > "src/$PKG/defs/$name/defs.yaml"
}

write_yaml "trino_io_manager" "type: $PKG.components.trino_io_manager.component.TrinoIOManagerComponent
attributes:
  resource_key: io_manager
  host: localhost
  port: $TRINO_PORT
  user: dagster
  catalog: memory
  default_schema: demo"

write_yaml "trino_resource" "type: $PKG.components.trino_resource.component.TrinoResourceComponent
attributes:
  resource_key: trino_resource
  host: localhost
  port: $TRINO_PORT
  user: dagster
  catalog: memory
  schema_name: demo"

write_yaml "synthetic_data_generator" "type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: synthetic_orders
  schema_type: orders
  row_count: 20
  random_state: 42
  group_name: trino_demo"

cat <<MSG

>>> Setup complete.
    cd $PROJECT_DIR
    uv run dg check defs
    # NB: trino_io_manager's partition-aware DELETE+INSERT writes need an
    # underlying catalog that supports DELETE. memory catalog only supports
    # CREATE+INSERT — full materialization may fail. dg check defs validates
    # that both components load + resolve correctly.
    uv run dg list defs

    # Validate Trino connectivity from inside:
    docker exec $TRINO_NAME trino --server localhost:8080 --execute 'SHOW CATALOGS;'

Stop + clean up:
    docker rm -f $TRINO_NAME
MSG
