#!/usr/bin/env bash
# Cassandra demo — exercise the Cassandra family against a single-node
# Cassandra container. No SaaS, no auth.

set -euo pipefail
if ! docker info >/dev/null 2>&1; then echo "ERROR: Docker daemon not running."; exit 1; fi

PROJECT_DIR="${1:-cassandra-demo}"
CASS_NAME=dg-cassandra-demo
CASS_PORT=9042
KEYSPACE=dagster_demo
TABLE=events

echo ">>> 1/5  Starting Cassandra in Docker on :$CASS_PORT (takes 60-90s)"
docker rm -f "$CASS_NAME" >/dev/null 2>&1 || true
sleep 1
docker run -d --name "$CASS_NAME" -p $CASS_PORT:9042 \
  -e CASSANDRA_CLUSTER_NAME=DagsterDemo \
  cassandra:5 >/dev/null

echo "    Waiting for Cassandra cluster to become ready..."
for i in $(seq 1 40); do
  if docker exec "$CASS_NAME" cqlsh -e 'DESC KEYSPACES' >/dev/null 2>&1; then
    echo "    Cassandra up."
    break
  fi
  sleep 5
done

echo ">>> 2/5  Creating keyspace + table + seeding 10 rows"
docker exec "$CASS_NAME" cqlsh -e "CREATE KEYSPACE IF NOT EXISTS $KEYSPACE WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1};" >/dev/null
docker exec "$CASS_NAME" cqlsh -e "CREATE TABLE IF NOT EXISTS $KEYSPACE.$TABLE (event_id int PRIMARY KEY, user text, event_type text, amount int);" >/dev/null
for i in 1 2 3 4 5 6 7 8 9 10; do
  docker exec "$CASS_NAME" cqlsh -e "INSERT INTO $KEYSPACE.$TABLE (event_id, user, event_type, amount) VALUES ($i, 'u$((i%3))', 'click', $((i*5)));" >/dev/null
done
echo "    Seeded 10 rows."

echo ">>> 3/5  Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q 'cassandra-driver>=3.25.0' pandas

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> 4/5  Installing 4 components"
for c in synthetic_data_generator cassandra_resource cassandra_reader cassandra_writer; do
  $CLI add $c --auto-install
done

echo ">>> 5/5  Writing defs.yaml"

write_yaml() {
  local name="$1"; shift
  local body="$1"; shift
  mkdir -p "src/$PKG/defs/$name"
  printf "%s\n" "$body" > "src/$PKG/defs/$name/defs.yaml"
}

write_yaml "cassandra_resource" "type: $PKG.components.cassandra_resource.component.CassandraResourceComponent
attributes:
  resource_key: cassandra_resource
  hosts: localhost
  port: $CASS_PORT
  keyspace: $KEYSPACE"

write_yaml "cassandra_reader" "type: $PKG.components.cassandra_reader.component.CassandraReaderComponent
attributes:
  asset_name: cassandra_events_read
  hosts: [localhost]
  port: $CASS_PORT
  keyspace: $KEYSPACE
  query: 'SELECT event_id, user, event_type, amount FROM $TABLE'
  group_name: cassandra_demo"

cat > .env.demo <<EOF
# No env vars required for this no-auth demo.
EOF

cat <<MSG

>>> Setup complete.
    cd $PROJECT_DIR
    uv run dg check defs
    uv run dg launch --assets cassandra_events_read

Stop + clean up:
    docker rm -f $CASS_NAME
MSG
