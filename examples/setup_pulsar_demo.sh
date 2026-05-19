#!/usr/bin/env bash
# Pulsar demo — exercise the Pulsar family against apachepulsar/pulsar in
# standalone mode. No SaaS, no auth.
#
# Components: pulsar_to_database_asset, pulsar_monitor, pulsar_observation_sensor
# + python_callable_job (target).

set -euo pipefail
if ! docker info >/dev/null 2>&1; then echo "ERROR: Docker daemon not running."; exit 1; fi

PROJECT_DIR="${1:-pulsar-demo}"
PULSAR_NAME=dg-pulsar-demo
PULSAR_PORT=6650
PULSAR_HTTP_PORT=8081
TOPIC=persistent://public/default/events
DB_PATH="/tmp/${PROJECT_DIR}.db"

echo ">>> 1/5  Starting Pulsar in Docker (standalone mode) on :$PULSAR_PORT (HTTP :$PULSAR_HTTP_PORT)"
docker rm -f "$PULSAR_NAME" >/dev/null 2>&1 || true
sleep 1
docker run -d --name "$PULSAR_NAME" \
  -p $PULSAR_PORT:6650 -p $PULSAR_HTTP_PORT:8080 \
  apachepulsar/pulsar:latest bin/pulsar standalone >/dev/null

echo "    Waiting for Pulsar to become ready (takes 25-45s on first run)..."
for i in $(seq 1 30); do
  if docker exec "$PULSAR_NAME" /pulsar/bin/pulsar-admin clusters list >/dev/null 2>&1; then
    echo "    Pulsar up after ${i}x3s."
    break
  fi
  sleep 3
done

echo ">>> 2/5  Seeding 25 messages (via Python producer — pulsar-client CLI's"
echo "        --separator flag is broken; -m splits JSON char-by-char regardless)"
# The pulsar-client CLI's `-m` flag has a longstanding bug where it splits
# input messages on every byte when --separator doesn't match. JSON payloads
# come out as 1-char messages. Use pulsar-py from a sidecar python image instead.
docker run --rm --network host -e PULSAR_URL="pulsar://localhost:$PULSAR_PORT" -e TOPIC="$TOPIC" \
  python:3.11-slim sh -c "
    pip install -q pulsar-client >/dev/null 2>&1
    python -c '
import os, json, pulsar
c = pulsar.Client(os.environ[\"PULSAR_URL\"])
p = c.create_producer(os.environ[\"TOPIC\"])
for i in range(1, 26):
    p.send(json.dumps({\"event_id\": i, \"type\": \"click\"}).encode(\"utf-8\"))
p.close(); c.close()
print(\"Produced 25 messages.\")
'
"

echo ">>> 3/5  Scaffolding Dagster project at $PROJECT_DIR"
rm -f "$DB_PATH"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q 'pulsar-client>=3.3.0' pandas 'sqlalchemy>=2.0.0'

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> 4/5  Installing 4 components"
for c in pulsar_to_database_asset pulsar_monitor pulsar_observation_sensor python_callable_job; do
  $CLI add $c --auto-install
done

echo ">>> 5/5  Writing defs.yaml"

write_yaml() {
  local name="$1"; shift
  local body="$1"; shift
  mkdir -p "src/$PKG/defs/$name"
  printf "%s\n" "$body" > "src/$PKG/defs/$name/defs.yaml"
}

write_yaml "pulsar_to_database_asset" "type: $PKG.components.pulsar_to_database_asset.component.PulsarToDatabaseAssetComponent
attributes:
  asset_name: pulsar_events_ingest
  service_url: pulsar://localhost:$PULSAR_PORT
  topic: $TOPIC
  subscription_name: dagster-ingest
  database_url: sqlite:///$DB_PATH
  table_name: raw_events
  max_messages: 100
  group_name: pulsar_demo"

write_yaml "python_callable_job" "type: $PKG.components.python_callable_job.component.PythonCallableJobComponent
attributes:
  job_name: process_pulsar_messages
  callable_path: os.path:exists
  kwargs: { path: /tmp }"

write_yaml "pulsar_monitor" "type: $PKG.components.pulsar_monitor.component.PulsarMonitorSensorComponent
attributes:
  sensor_name: pulsar_events_sensor
  service_url: pulsar://localhost:$PULSAR_PORT
  topic: $TOPIC
  subscription_name: dagster-sensor
  job_name: process_pulsar_messages
  subscription_type: Shared
  max_messages_per_poll: 50
  receive_timeout_ms: 5000
  minimum_interval_seconds: 30
  default_status: stopped"

write_yaml "pulsar_observation_sensor" "type: $PKG.components.pulsar_observation_sensor.component.PulsarObservationSensorComponent
attributes:
  sensor_name: pulsar_events_obs
  asset_key: streaming/pulsar_events
  service_url: pulsar://localhost:$PULSAR_PORT
  topic: $TOPIC
  check_interval_seconds: 60"

cat <<MSG

>>> Setup complete.
    cd $PROJECT_DIR
    uv run dg check defs
    uv run dg launch --assets pulsar_events_ingest
    sqlite3 $DB_PATH 'SELECT COUNT(*) FROM raw_events;'    # 25

Stop + clean up:
    docker rm -f $PULSAR_NAME
MSG
