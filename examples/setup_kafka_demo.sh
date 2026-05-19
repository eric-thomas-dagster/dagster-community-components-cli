#!/usr/bin/env bash
# Kafka demo — full pipeline against a local Kafka broker (KRaft mode, no Zookeeper).
# Validates the entire Kafka family of community components end-to-end with
# no SaaS, no auth, no managed cluster.
#
# WHAT THIS DEMONSTRATES
#   The 5 Kafka components wired together in one project:
#     - external_kafka_asset       (declare-only AssetSpec for the topic)
#     - kafka_resource             (resource shared by downstream components)
#     - kafka_to_database_asset    (consume topic → SQLite via SQLAlchemy)
#     - kafka_monitor              (sensor: poll topic, trigger a job on new messages)
#     - kafka_observation_sensor   (emit AssetObservations on external_kafka_asset)
#
#   Plus python_callable_job as the target the kafka_monitor sensor triggers.
#
# Asset graph:
#   external/events                  ← external_kafka_asset (declare-only)
#   kafka_events_ingest              ← kafka_to_database_asset (Kafka → SQLite)
#   (job: process_kafka_messages)    ← python_callable_job (target for sensor)
#
# REQUIRES: Docker daemon running. The setup script starts a single Kafka
# container in KRaft mode (no Zookeeper) on localhost:9092.
#
# COST: \$0 — runs entirely on your laptop in Docker.

set -euo pipefail

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker daemon is not running."
  echo "Start Docker Desktop (or 'colima start') and re-run."
  exit 1
fi

PROJECT_DIR="${1:-kafka-demo}"
KAFKA_NAME=dg-kafka-demo
KAFKA_PORT=9092
TOPIC=events
DB_PATH="/tmp/${PROJECT_DIR}.db"

echo ">>> 1/6  Starting Kafka in Docker ($KAFKA_NAME:$KAFKA_PORT, KRaft mode)"
docker rm -f "$KAFKA_NAME" >/dev/null 2>&1 || true
docker run -d --name "$KAFKA_NAME" -p $KAFKA_PORT:9092 \
  apache/kafka:latest >/dev/null

echo "    Waiting for Kafka to become ready..."
for i in 1 2 3 4 5 6 7 8 9 10; do
  if docker exec "$KAFKA_NAME" /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --list >/dev/null 2>&1; then
    echo "    Kafka up."
    break
  fi
  sleep 3
done

echo ">>> 2/6  Creating topic '$TOPIC' + seeding 50 JSON events"
docker exec "$KAFKA_NAME" /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 \
  --create --if-not-exists --topic "$TOPIC" --partitions 1 --replication-factor 1 >/dev/null

# Produce 50 synthetic JSON messages
docker exec -i "$KAFKA_NAME" sh -c "
  for i in \$(seq 1 50); do
    echo \"{\\\"event_id\\\":\$i,\\\"user_id\\\":\\\"user_\$((i % 7))\\\",\\\"event_type\\\":\\\"click\\\",\\\"ts\\\":\\\"2026-05-14T12:00:0\$((i % 10))\\\"}\"
  done | /opt/kafka/bin/kafka-console-producer.sh --bootstrap-server localhost:9092 --topic $TOPIC
"
echo "    Seeded 50 messages on topic '$TOPIC'."

echo ">>> 3/6  Scaffolding Dagster project at $PROJECT_DIR"
rm -f "$DB_PATH"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q 'confluent-kafka>=2.0.0' 'kafka-python>=2.0.2' sqlalchemy

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> 4/6  Installing 6 components (5 Kafka + 1 target job)"
for c in external_kafka_asset kafka_resource kafka_to_database_asset \
         kafka_monitor kafka_observation_sensor python_callable_job; do
  $CLI add $c --auto-install
done

echo ">>> 5/6  Writing defs.yaml for the Kafka pipeline"

write_yaml() {
  local name="$1"; shift
  local body="$1"; shift
  mkdir -p "src/$PKG/defs/$name"
  printf "%s\n" "$body" > "src/$PKG/defs/$name/defs.yaml"
}

# Declare the topic as an external asset (declare-only)
write_yaml "external_kafka_asset" "type: $PKG.components.external_kafka_asset.component.ExternalKafkaAsset
attributes:
  asset_key: external/events
  bootstrap_servers: localhost:$KAFKA_PORT
  topic: $TOPIC
  group_name: kafka_demo"

# Shared resource (used by sensors / observation)
write_yaml "kafka_resource" "type: $PKG.components.kafka_resource.component.KafkaResourceComponent
attributes:
  resource_key: kafka_resource
  bootstrap_servers: localhost:$KAFKA_PORT
  security_protocol: PLAINTEXT"

# Consume topic → write to SQLite
write_yaml "kafka_to_database_asset" "type: $PKG.components.kafka_to_database_asset.component.KafkaToDatabaseAssetComponent
attributes:
  asset_name: kafka_events_ingest
  bootstrap_servers: localhost:$KAFKA_PORT
  database_url: sqlite:///$DB_PATH
  topic: $TOPIC
  consumer_group: dagster-demo-ingestion
  table_name: raw_events
  max_messages: 100
  poll_timeout_seconds: 5
  if_exists: replace
  security_protocol: PLAINTEXT
  group_name: kafka_demo"

# Target job that kafka_monitor will trigger when new messages arrive
write_yaml "python_callable_job" "type: $PKG.components.python_callable_job.component.PythonCallableJobComponent
attributes:
  job_name: process_kafka_messages
  callable_path: os.path:exists
  kwargs:
    path: /tmp"

# Sensor: poll Kafka, trigger process_kafka_messages on new messages
write_yaml "kafka_monitor" "type: $PKG.components.kafka_monitor.component.KafkaMonitorSensorComponent
attributes:
  sensor_name: kafka_events_sensor
  bootstrap_servers: localhost:$KAFKA_PORT
  topic: $TOPIC
  group_id: dagster-demo-sensor
  job_name: process_kafka_messages
  minimum_interval_seconds: 30
  max_messages_per_poll: 50
  poll_timeout_seconds: 5.0
  security_protocol: PLAINTEXT
  default_status: stopped"

# Observation sensor on the external asset
write_yaml "kafka_observation_sensor" "type: $PKG.components.kafka_observation_sensor.component.KafkaObservationSensorComponent
attributes:
  sensor_name: kafka_events_observation
  asset_key: external/events
  bootstrap_servers: localhost:$KAFKA_PORT
  topic: $TOPIC
  check_interval_seconds: 60"

cat <<MSG

>>> Setup complete.

Validate everything loaded:
    cd $PROJECT_DIR
    uv run dg check defs
    uv run dg list defs   # 2 assets, 1 job, 2 sensors

Drain the topic into SQLite:
    uv run dg launch --assets kafka_events_ingest

After the run, check what landed:
    sqlite3 $DB_PATH 'SELECT COUNT(*) FROM raw_events;'    # should be 50
    sqlite3 $DB_PATH 'SELECT * FROM raw_events LIMIT 3;'

Browse it in the UI (sensors stay STOPPED until you toggle them on):
    uv run dg dev   # http://localhost:3000 → Assets / Jobs / Sensors

Stop + clean up Kafka:
    docker rm -f $KAFKA_NAME

This demo uses Kafka in KRaft mode — a single-process broker with no
Zookeeper. The same components work unchanged against MSK, Confluent
Cloud, Strimzi-on-Kubernetes, or self-hosted clusters: just change
bootstrap_servers + security_protocol (and add sasl_* env vars).
MSG
