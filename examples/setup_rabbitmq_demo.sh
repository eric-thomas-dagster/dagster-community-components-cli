#!/usr/bin/env bash
# RabbitMQ demo — exercise the RabbitMQ family against a local broker
# running in Docker. No SaaS, no auth, no managed cluster.
#
# Components: rabbitmq_to_database_asset, rabbitmq_monitor,
# rabbitmq_observation_sensor + python_callable_job (sensor target).
#
# Asset graph:
#   external/queue:orders   ← declared by rabbitmq_observation_sensor.asset_key
#   rabbitmq_orders_ingest  ← rabbitmq_to_database_asset (queue → SQLite)
#
# REQUIRES: Docker daemon.
# COST: \$0 — rabbitmq:3.13-management, ~250 MB.

set -euo pipefail

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker daemon is not running."; exit 1
fi

PROJECT_DIR="${1:-rabbitmq-demo}"
RMQ_NAME=dg-rabbitmq-demo
RMQ_PORT=5672
RMQ_MGMT_PORT=15672
QUEUE=orders
DB_PATH="/tmp/${PROJECT_DIR}.db"

echo ">>> 1/5  Starting RabbitMQ in Docker ($RMQ_NAME:$RMQ_PORT, mgmt:$RMQ_MGMT_PORT)"
docker rm -f "$RMQ_NAME" >/dev/null 2>&1 || true
sleep 2   # let docker release prior mounts
# --hostname + --user pin the .erlang.cookie owner consistently; without
# them, Docker Desktop's VirtioFS can write the cookie with mismatched
# perms on subsequent boots and rabbit fails with eacces.
docker run -d --name "$RMQ_NAME" --hostname rabbit \
  --user rabbitmq:rabbitmq \
  -p $RMQ_PORT:5672 -p $RMQ_MGMT_PORT:15672 \
  rabbitmq:4-management >/dev/null

echo "    Waiting for RabbitMQ to become ready (takes 15-25s on first run)..."
for i in $(seq 1 20); do
  if docker exec "$RMQ_NAME" rabbitmq-diagnostics ping >/dev/null 2>&1; then
    echo "    RabbitMQ up. Mgmt UI: http://localhost:$RMQ_MGMT_PORT (guest/guest)"
    break
  fi
  sleep 3
done

echo ">>> 2/5  Declaring queue '$QUEUE' + seeding 30 JSON messages"
# rabbitmqadmin v4 ships with rabbitmq:4-management. Different CLI from v3:
# `--name` instead of `name=`, `publish message --payload` instead of HTTP API.
docker exec "$RMQ_NAME" rabbitmqadmin declare queue --name "$QUEUE" --type classic --durable true >/dev/null

docker exec "$RMQ_NAME" sh -c "
  for i in \$(seq 1 30); do
    rabbitmqadmin publish message --routing-key $QUEUE \
      --payload \"{\\\"order_id\\\":\$i,\\\"customer\\\":\\\"c\$((i % 5))\\\",\\\"amount\\\":\$((i * 10))}\" >/dev/null
  done
"
echo "    Seeded 30 messages."

echo ">>> 3/5  Scaffolding Dagster project at $PROJECT_DIR"
rm -f "$DB_PATH"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q 'pika>=1.3.0' pandas 'sqlalchemy>=2.0.0'

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> 4/5  Installing 4 components"
for c in rabbitmq_to_database_asset rabbitmq_monitor rabbitmq_observation_sensor python_callable_job; do
  $CLI add $c --auto-install
done

echo ">>> 5/5  Writing defs.yaml"

write_yaml() {
  local name="$1"; shift
  local body="$1"; shift
  mkdir -p "src/$PKG/defs/$name"
  printf "%s\n" "$body" > "src/$PKG/defs/$name/defs.yaml"
}

write_yaml "rabbitmq_to_database_asset" "type: $PKG.components.rabbitmq_to_database_asset.component.RabbitMQToDatabaseAssetComponent
attributes:
  asset_name: rabbitmq_orders_ingest
  amqp_url: amqp://guest:guest@localhost:$RMQ_PORT/
  queue_name: $QUEUE
  database_url: sqlite:///$DB_PATH
  table_name: raw_orders
  max_messages: 100
  group_name: rabbitmq_demo"

write_yaml "python_callable_job" "type: $PKG.components.python_callable_job.component.PythonCallableJobComponent
attributes:
  job_name: process_rabbitmq_messages
  callable_path: os.path:exists
  kwargs:
    path: /tmp"

write_yaml "rabbitmq_monitor" "type: $PKG.components.rabbitmq_monitor.component.RabbitMQMonitorSensorComponent
attributes:
  sensor_name: rabbitmq_orders_sensor
  host: localhost
  queue_name: $QUEUE
  job_name: process_rabbitmq_messages
  port: $RMQ_PORT
  virtual_host: /
  use_tls: false
  max_messages_per_poll: 50
  minimum_interval_seconds: 30
  default_status: stopped"

write_yaml "rabbitmq_observation_sensor" "type: $PKG.components.rabbitmq_observation_sensor.component.RabbitmqObservationSensorComponent
attributes:
  sensor_name: rabbitmq_orders_obs
  asset_key: queue/orders
  host: localhost
  queue_name: $QUEUE
  check_interval_seconds: 60"

cat <<MSG

>>> Setup complete.

    cd $PROJECT_DIR
    uv run dg check defs
    uv run dg launch --assets rabbitmq_orders_ingest

After the run:
    sqlite3 $DB_PATH 'SELECT COUNT(*) FROM raw_orders;'   # should be 30

Stop + clean up:
    docker rm -f $RMQ_NAME
MSG
