#!/usr/bin/env bash
# InfluxDB 2.x end-to-end demo — single-container Docker.
#
# WHAT THIS DEMONSTRATES
#   The 2 InfluxDB community components shipped in v0.10.1:
#
#     1. `influxdb_resource`     connection resource (official influxdb-client SDK)
#     2. `dataframe_to_influxdb` bulk-write a Pandas DataFrame as line-protocol
#                                points; auto-classifies tags vs fields by dtype
#
# Uses synthetic_data_generator (schema_type: sensors) to produce time-series.
# After materialize, the data is queryable via Flux at port 18086.
#
# COST: $0 — InfluxDB 2.x in Docker with auto-initialized org + bucket + token.

set -euo pipefail
PROJECT_DIR="${1:-influxdb-demo}"
INFLUX_PORT="${INFLUX_PORT:-18086}"
INFLUX_CONTAINER="${INFLUX_CONTAINER:-influxdb-demo-server}"
INFLUX_ORG="${INFLUX_ORG:-dagster-org}"
INFLUX_BUCKET="${INFLUX_BUCKET:-metrics}"
INFLUX_USER="${INFLUX_USER:-admin}"
INFLUX_PASSWORD="${INFLUX_PASSWORD:-influxdb-admin}"
INFLUX_TOKEN="${INFLUX_TOKEN:-dagster-demo-token-do-not-use-in-prod}"

# --- 1. Bring up InfluxDB ---
echo ">>> Starting InfluxDB 2.x in Docker (container: $INFLUX_CONTAINER)"
docker rm -f "$INFLUX_CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$INFLUX_CONTAINER" \
  -p "$INFLUX_PORT:8086" \
  -e DOCKER_INFLUXDB_INIT_MODE=setup \
  -e DOCKER_INFLUXDB_INIT_USERNAME="$INFLUX_USER" \
  -e DOCKER_INFLUXDB_INIT_PASSWORD="$INFLUX_PASSWORD" \
  -e DOCKER_INFLUXDB_INIT_ORG="$INFLUX_ORG" \
  -e DOCKER_INFLUXDB_INIT_BUCKET="$INFLUX_BUCKET" \
  -e DOCKER_INFLUXDB_INIT_ADMIN_TOKEN="$INFLUX_TOKEN" \
  influxdb:2-alpine >/dev/null

echo ">>> Waiting for InfluxDB to come up..."
for i in $(seq 1 30); do
  if curl -sS "http://localhost:$INFLUX_PORT/ping" 2>/dev/null -o /dev/null -w "%{http_code}" | grep -q "204"; then
    echo "    ready after ${i}s"
    break
  fi
  sleep 1
done

# --- 2. Scaffold the Dagster project ---
echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q 'yarl<1.24' pandas 'influxdb-client>=1.40.0'
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing components (--refresh on first call busts the registry-manifest cache)"
$CLI --refresh add synthetic_data_generator --auto-install
$CLI add influxdb_resource --auto-install
$CLI add dataframe_to_influxdb --auto-install

echo ">>> Overwriting CLI-installed example defs.yamls with demo-specific config"

# synthetic_data_generator → produce IoT sensor time-series.
# `sensors` schema emits: sensor_id, timestamp, sensor_type, location, value,
# unit, status — the dataframe_to_influxdb sink auto-classifies numeric (value)
# as field and the rest as tags.
cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: sensor_readings
  schema_type: sensors
  row_count: 10000
  group_name: source
EOF

# dataframe_to_influxdb → write to the `sensor_reading` measurement in bucket.
cat > "src/$PKG/defs/dataframe_to_influxdb/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_influxdb.component.DataframeToInfluxDBComponent
attributes:
  asset_name: influxdb_sensor_write
  upstream_asset_key: sensor_readings
  measurement: sensor_reading
  bucket: $INFLUX_BUCKET
  url_env_var: INFLUXDB_URL
  token_env_var: INFLUXDB_TOKEN
  org_env_var: INFLUXDB_ORG
  timestamp_column: timestamp
  tag_columns: [sensor_id, sensor_type, location, unit, status]
  field_columns: [value]
  group_name: influxdb
EOF

# influxdb_resource — connection (the sink uses env-vars directly above; the
# resource is here for catalog discoverability + future demo expansion).
cat > "src/$PKG/defs/influxdb_resource/defs.yaml" <<EOF
type: $PKG.components.influxdb_resource.component.InfluxDBResourceComponent
attributes:
  resource_key: influxdb_resource
  url: http://localhost:$INFLUX_PORT
  token_env_var: INFLUXDB_TOKEN
  org: $INFLUX_ORG
  bucket: $INFLUX_BUCKET
EOF

# --- 3. Final instructions ---
echo ""
echo "============================================================"
echo "InfluxDB 2.x demo scaffolded at $PROJECT_DIR"
echo "============================================================"
echo ""
echo "InfluxDB UI:  http://localhost:$INFLUX_PORT"
echo "  Login: $INFLUX_USER / $INFLUX_PASSWORD"
echo "  Org:   $INFLUX_ORG"
echo "  Bucket: $INFLUX_BUCKET"
echo "  Token: $INFLUX_TOKEN  (demo-only — pre-seeded)"
echo ""
echo "Next steps:"
echo ""
echo "  cd $PROJECT_DIR"
echo "  export INFLUXDB_URL=http://localhost:$INFLUX_PORT"
echo "  export INFLUXDB_TOKEN='$INFLUX_TOKEN'"
echo "  export INFLUXDB_ORG='$INFLUX_ORG'"
echo ""
echo "  uv run dg launch --assets '*'             # headless materialize"
echo "  # OR uv run dg dev                        # UI at http://localhost:3000"
echo ""
echo "Then verify via Flux:"
echo "  curl -X POST 'http://localhost:$INFLUX_PORT/api/v2/query?org=$INFLUX_ORG' \\"
echo "    -H 'Authorization: Token $INFLUX_TOKEN' \\"
echo "    -H 'Content-Type: application/vnd.flux' \\"
echo "    --data 'from(bucket:\"$INFLUX_BUCKET\") |> range(start: -25h) |> filter(fn:(r) => r._measurement == \"sensor_reading\") |> count()'"
echo ""
echo "Cleanup:"
echo "  docker rm -f $INFLUX_CONTAINER"
echo ""
