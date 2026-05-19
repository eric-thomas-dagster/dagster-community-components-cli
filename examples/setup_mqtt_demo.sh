#!/usr/bin/env bash
# MQTT demo — exercise the MQTT family against a local eclipse-mosquitto broker
# running in Docker. No SaaS, no auth.
#
# Components: mqtt_to_database_asset, mqtt_monitor, mqtt_observation_sensor
# + python_callable_job (target).

set -euo pipefail
if ! docker info >/dev/null 2>&1; then echo "ERROR: Docker daemon not running."; exit 1; fi

PROJECT_DIR="${1:-mqtt-demo}"
MQTT_NAME=dg-mqtt-demo
MQTT_PORT=1883
TOPIC=sensors/factory/temp
DB_PATH="/tmp/${PROJECT_DIR}.db"

echo ">>> 1/5  Starting Mosquitto in Docker on :$MQTT_PORT"
docker rm -f "$MQTT_NAME" >/dev/null 2>&1 || true
sleep 1
# Mosquitto 2.x requires explicit anonymous config — pass it inline
docker run -d --name "$MQTT_NAME" -p $MQTT_PORT:1883 \
  eclipse-mosquitto:2 sh -c "
    echo 'listener 1883' > /mosquitto/config/mosquitto.conf
    echo 'allow_anonymous true' >> /mosquitto/config/mosquitto.conf
    exec /usr/sbin/mosquitto -c /mosquitto/config/mosquitto.conf
  " >/dev/null
sleep 3

echo ">>> 2/5  Pre-publishing 30 sensor readings (retained, so the consumer can backfill)"
docker run --rm --network host eclipse-mosquitto:2 sh -c "
  for i in \$(seq 1 30); do
    mosquitto_pub -h localhost -p $MQTT_PORT -t '$TOPIC' -m \"{\\\"sensor_id\\\":\$i,\\\"temp_c\\\":\$((20 + i % 10)),\\\"ts\\\":\\\"2026-05-14T12:00:\$((i % 60))\\\"}\" -q 1
  done
  echo 'Published 30 messages.'
"

echo ">>> 3/5  Scaffolding Dagster project at $PROJECT_DIR"
rm -f "$DB_PATH"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q 'paho-mqtt>=1.6.0' pandas 'sqlalchemy>=2.0.0'

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> 4/5  Installing 4 components"
for c in mqtt_to_database_asset mqtt_monitor mqtt_observation_sensor python_callable_job; do
  $CLI add $c --auto-install
done

echo ">>> 5/5  Writing defs.yaml"

write_yaml() {
  local name="$1"; shift
  local body="$1"; shift
  mkdir -p "src/$PKG/defs/$name"
  printf "%s\n" "$body" > "src/$PKG/defs/$name/defs.yaml"
}

# Need an upstream concurrent publisher: MQTT is fire-and-forget broker, so an
# asset that subscribes to a quiet topic will time out. Re-publish during the run.
mkdir -p "src/$PKG/seed"
cat > "src/$PKG/seed/__init__.py" <<'PY'
"""Helper: republishes 30 messages on each call. The mqtt_to_database_asset
   subscribes with collect_seconds=10, so we kick off a publisher right before
   each run via a python_callable_job set to run first (deps wire-up below)."""
import time
def publish_burst():
    import paho.mqtt.client as mqtt
    c = mqtt.Client()
    c.connect("localhost", 1883, 60)
    c.loop_start()
    time.sleep(0.5)
    for i in range(1, 31):
        c.publish("sensors/factory/temp", '{"sensor_id":%d,"temp_c":%d}' % (i, 20 + (i % 10)), qos=1)
    time.sleep(1)
    c.loop_stop()
    c.disconnect()
    return {"published": 30}
PY

write_yaml "mqtt_to_database_asset" "type: $PKG.components.mqtt_to_database_asset.component.MQTTToDatabaseAssetComponent
attributes:
  asset_name: mqtt_sensors_ingest
  broker_host_env_var: MQTT_BROKER_HOST
  topic: $TOPIC
  database_url_env_var: DATABASE_URL
  table_name: raw_sensor_readings
  collect_seconds: 15
  max_messages: 100
  group_name: mqtt_demo"

write_yaml "python_callable_job" "type: $PKG.components.python_callable_job.component.PythonCallableJobComponent
attributes:
  job_name: process_mqtt_messages
  callable_path: os.path:exists
  kwargs: { path: /tmp }"

write_yaml "mqtt_monitor" "type: $PKG.components.mqtt_monitor.component.MQTTMonitorSensorComponent
attributes:
  sensor_name: mqtt_telemetry_sensor
  broker_host: localhost
  topic: $TOPIC
  job_name: process_mqtt_messages
  broker_port: $MQTT_PORT
  qos: 1
  collect_window_seconds: 5.0
  max_messages_per_poll: 50
  use_tls: false
  minimum_interval_seconds: 60
  default_status: stopped"

write_yaml "mqtt_observation_sensor" "type: $PKG.components.mqtt_observation_sensor.component.MqttObservationSensorComponent
attributes:
  sensor_name: mqtt_telemetry_obs
  asset_key: streaming/mqtt_telemetry
  broker_host: localhost
  topic: $TOPIC
  check_interval_seconds: 60"

cat > .env.demo <<EOF
export MQTT_BROKER_HOST='localhost'
export DATABASE_URL='sqlite:///$DB_PATH'
EOF

cat <<MSG

>>> Setup complete.

Validate + materialize:
    cd $PROJECT_DIR && source .env.demo
    uv run dg check defs
    # MQTT is fire-and-forget. Pre-published messages are gone if not QoS retained.
    # Run a publisher in one shell, the asset in another (collect_seconds=15):
    docker run --rm --network host eclipse-mosquitto:2 sh -c '
      for i in \$(seq 1 30); do
        mosquitto_pub -h localhost -p $MQTT_PORT -t $TOPIC -m "{\"sensor_id\":\$i,\"temp_c\":\$((20+i%10))}" -q 1
        sleep 0.3
      done' &
    uv run dg launch --assets mqtt_sensors_ingest
    sqlite3 $DB_PATH 'SELECT COUNT(*) FROM raw_sensor_readings;'   # ~30

Stop + clean up:
    docker rm -f $MQTT_NAME
MSG
