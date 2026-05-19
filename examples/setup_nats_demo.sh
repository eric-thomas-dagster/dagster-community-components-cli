#!/usr/bin/env bash
# NATS demo — exercise the NATS family against a local nats-server (JetStream
# enabled) running in Docker. No SaaS, no auth, no managed cluster.
#
# Components: nats_to_database_asset, nats_monitor, nats_observation_sensor
# + python_callable_job (target).
#
# Asset graph:
#   nats_events_ingest  ← nats_to_database_asset (JetStream → SQLite)
#
# REQUIRES: Docker daemon.

set -euo pipefail
if ! docker info >/dev/null 2>&1; then echo "ERROR: Docker daemon not running."; exit 1; fi

PROJECT_DIR="${1:-nats-demo}"
NATS_NAME=dg-nats-demo
NATS_PORT=4222
STREAM=EVENTS
SUBJECT=events.demo
DB_PATH="/tmp/${PROJECT_DIR}.db"

echo ">>> 1/5  Starting NATS in Docker (JetStream enabled) on :$NATS_PORT"
docker rm -f "$NATS_NAME" >/dev/null 2>&1 || true
sleep 1
docker run -d --name "$NATS_NAME" -p $NATS_PORT:4222 nats:latest -js >/dev/null
sleep 2

echo ">>> 2/5  Creating JetStream + seeding 25 messages (via natsio/nats-box sidecar)"
# nats:latest is a minimal distroless image with no shell + no `nats` CLI;
# we use the natsio/nats-box sidecar for stream + consumer creation and publishing.
# Pre-creating the durable consumer with --deliver=all so the Dagster asset can
# replay historical messages (JetStream's default deliver policy is 'new', which
# would skip messages published before the consumer existed).
docker run --rm --network host natsio/nats-box:latest sh -c "
  nats --server nats://localhost:$NATS_PORT stream add $STREAM --subjects=$SUBJECT --defaults --no-allow-rollup >/dev/null
  nats --server nats://localhost:$NATS_PORT consumer add $STREAM dagster-ingest --pull --deliver=all --ack=explicit --replay=instant --filter=$SUBJECT --max-deliver=-1 --max-pending=0 --no-headers-only --defaults >/dev/null
  for i in \$(seq 1 25); do
    nats --server nats://localhost:$NATS_PORT pub $SUBJECT \"{\\\"event_id\\\":\$i,\\\"type\\\":\\\"click\\\"}\" >/dev/null
  done
  echo 'Stream seeded.'
"

echo ">>> 3/5  Scaffolding Dagster project at $PROJECT_DIR"
rm -f "$DB_PATH"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q 'nats-py>=2.6.0' pandas 'sqlalchemy>=2.0.0'

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> 4/5  Installing 4 components"
for c in nats_to_database_asset nats_monitor nats_observation_sensor python_callable_job; do
  $CLI add $c --auto-install
done

echo ">>> 5/5  Writing defs.yaml"

write_yaml() {
  local name="$1"; shift
  local body="$1"; shift
  mkdir -p "src/$PKG/defs/$name"
  printf "%s\n" "$body" > "src/$PKG/defs/$name/defs.yaml"
}

write_yaml "nats_to_database_asset" "type: $PKG.components.nats_to_database_asset.component.NATSToDatabaseAssetComponent
attributes:
  asset_name: nats_events_ingest
  nats_url_env_var: NATS_URL
  subject: $SUBJECT
  use_jetstream: true
  stream_name: $STREAM
  consumer_name: dagster-ingest
  database_url_env_var: DATABASE_URL
  table_name: raw_events
  max_messages: 100
  group_name: nats_demo"

write_yaml "python_callable_job" "type: $PKG.components.python_callable_job.component.PythonCallableJobComponent
attributes:
  job_name: process_nats_messages
  callable_path: os.path:exists
  kwargs: { path: /tmp }"

write_yaml "nats_monitor" "type: $PKG.components.nats_monitor.component.NATSMonitorSensorComponent
attributes:
  sensor_name: nats_events_sensor
  servers: nats://localhost:$NATS_PORT
  stream_name: $STREAM
  consumer_name: dagster-sensor
  job_name: process_nats_messages
  max_messages_per_poll: 50
  fetch_timeout_seconds: 5.0
  minimum_interval_seconds: 30
  default_status: stopped"

write_yaml "nats_observation_sensor" "type: $PKG.components.nats_observation_sensor.component.NatsObservationSensorComponent
attributes:
  sensor_name: nats_events_obs
  asset_key: streaming/nats_events
  servers: nats://localhost:$NATS_PORT
  stream_name: $STREAM
  check_interval_seconds: 60"

cat > .env.demo <<EOF
export NATS_URL='nats://localhost:$NATS_PORT'
export DATABASE_URL='sqlite:///$DB_PATH'
EOF

cat <<MSG

>>> Setup complete.
    cd $PROJECT_DIR && source .env.demo
    uv run dg check defs
    uv run dg launch --assets nats_events_ingest
    sqlite3 $DB_PATH 'SELECT COUNT(*) FROM raw_events;'   # 25

Stop + clean up:
    docker rm -f $NATS_NAME
MSG
