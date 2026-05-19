#!/usr/bin/env bash
# Redis demo — exercise the Redis family of community components against a
# local Redis running in Docker. No SaaS, no auth, no managed cluster.
#
# WHAT THIS DEMONSTRATES
#   4 Redis components wired into one project:
#     - redis_resource                    (shared connection)
#     - redis_streams_monitor             (sensor: poll a Redis stream, trigger job)
#     - redis_stream_observation_sensor   (emit AssetObservation on a stream)
#     - cache_invalidation_job            (op job: FLUSH keys matching a pattern)
#
#   Plus python_callable_job as the target the streams_monitor triggers.
#
# Asset graph: none directly. This demonstrates the **sensors + jobs** side
# of the Redis family.
#   - external/events  ← (declared by redis_stream_observation_sensor's asset_key field)
#   - job process_stream_entries  ← python_callable_job (target of streams_monitor)
#   - job session_cache_flush     ← cache_invalidation_job (pattern: 'session:*')
#
# REQUIRES: Docker daemon running.
# COST: \$0 — single redis:7-alpine container, ~30 MB image.

set -euo pipefail

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker daemon is not running."
  echo "Start Docker Desktop (or 'colima start') and re-run."
  exit 1
fi

PROJECT_DIR="${1:-redis-demo}"
REDIS_NAME=dg-redis-demo
REDIS_PORT=6379

echo ">>> 1/5  Starting Redis in Docker ($REDIS_NAME:$REDIS_PORT)"
docker rm -f "$REDIS_NAME" >/dev/null 2>&1 || true
docker run -d --name "$REDIS_NAME" -p $REDIS_PORT:6379 redis:7-alpine >/dev/null

echo "    Waiting for Redis to become ready..."
for i in 1 2 3 4 5; do
  if docker exec "$REDIS_NAME" redis-cli ping 2>/dev/null | grep -q PONG; then
    echo "    Redis up."
    break
  fi
  sleep 1
done

echo ">>> 2/5  Seeding Redis: 5 stream entries + 3 session cache keys"
docker exec "$REDIS_NAME" sh -c '
  for i in 1 2 3 4 5; do
    redis-cli XADD events:incoming "*" event_id "$i" type click user "u$i" >/dev/null
  done
  for k in session:abc session:def session:ghi; do
    redis-cli SET "$k" "userdata-$k" >/dev/null
  done
  echo "stream length: $(redis-cli XLEN events:incoming)"
  echo "session keys:  $(redis-cli KEYS \"session:*\" | wc -l | tr -d \" \")"
'

echo ">>> 3/5  Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q 'redis>=4.0.0'

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> 4/5  Installing 5 components"
for c in redis_resource redis_streams_monitor redis_stream_observation_sensor \
         cache_invalidation_job python_callable_job; do
  $CLI add $c --auto-install
done

echo ">>> 5/5  Writing defs.yaml for the Redis pipeline"

write_yaml() {
  local name="$1"; shift
  local body="$1"; shift
  mkdir -p "src/$PKG/defs/$name"
  printf "%s\n" "$body" > "src/$PKG/defs/$name/defs.yaml"
}

# Shared Redis resource
write_yaml "redis_resource" "type: $PKG.components.redis_resource.component.RedisResourceComponent
attributes:
  resource_key: redis_resource
  host: localhost
  port: $REDIS_PORT
  db: 0
  ssl: false"

# Target job for the streams monitor
write_yaml "python_callable_job" "type: $PKG.components.python_callable_job.component.PythonCallableJobComponent
attributes:
  job_name: process_stream_entries
  callable_path: os.path:exists
  kwargs:
    path: /tmp"

# Sensor: poll Redis stream, trigger process_stream_entries when new entries appear
write_yaml "redis_streams_monitor" "type: $PKG.components.redis_streams_monitor.component.RedisStreamsMonitorSensorComponent
attributes:
  sensor_name: events_stream_sensor
  stream_name: events:incoming
  job_name: process_stream_entries
  host: localhost
  port: $REDIS_PORT
  db: 0
  max_entries_per_poll: 50
  start_from_beginning: true
  minimum_interval_seconds: 30
  default_status: stopped"

# Observation sensor: emit AssetObservation on each check
write_yaml "redis_stream_observation_sensor" "type: $PKG.components.redis_stream_observation_sensor.component.RedisStreamObservationSensorComponent
attributes:
  sensor_name: events_stream_obs
  asset_key: streaming/events
  stream_name: events:incoming
  check_interval_seconds: 60"

# Cache invalidation: flush session:* keys
write_yaml "cache_invalidation_job" "type: $PKG.components.cache_invalidation_job.component.CacheInvalidationJobComponent
attributes:
  job_name: session_cache_flush
  redis_url_env: REDIS_URL
  pattern: 'session:*'
  db: 0
  batch_size: 500"

echo "export REDIS_URL='redis://localhost:$REDIS_PORT/0'" > .env.demo

cat <<MSG

>>> Setup complete.

Validate everything loaded:
    cd $PROJECT_DIR
    source .env.demo
    uv run dg check defs
    uv run dg list defs   # 1 resource, 2 jobs, 2 sensors

Run the cache invalidation job — should flush the 3 session:* keys:
    uv run dg launch --job session_cache_flush
    docker exec $REDIS_NAME redis-cli KEYS 'session:*'   # should return nothing

Run the placeholder target job:
    uv run dg launch --job process_stream_entries

Browse it in the UI (sensors stay STOPPED until you toggle them on):
    uv run dg dev   # http://localhost:3000

Stop + clean up:
    docker rm -f $REDIS_NAME
MSG
