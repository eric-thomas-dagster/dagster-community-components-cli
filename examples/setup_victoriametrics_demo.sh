#!/usr/bin/env bash
# VictoriaMetrics end-to-end demo — single-container Docker.
#
# WHAT THIS DEMONSTRATES
#   The 3 VictoriaMetrics community components shipped in v0.10.1:
#
#     1. `victoriametrics_resource`     connection resource (HTTP endpoints + auth)
#     2. `dataframe_to_victoriametrics` bulk ingest a Pandas DataFrame as
#                                       time-series via /api/v1/import/prometheus
#     3. `victoriametrics_query_asset`  PromQL query → Pandas DataFrame
#
# + uses `synthetic_data_generator` to produce time-series data the sink
#   consumes.
#
# Asset graph:
#   synthetic_data_generator (metrics_timeseries, ~10k samples)
#         │
#         ▼
#   dataframe_to_victoriametrics (ingest into VM)
#         │
#         ▼
#   victoriametrics_query_asset (PromQL read-back into a DataFrame)
#
# COST: $0 — runs VictoriaMetrics single-node in Docker on port 8428.

set -euo pipefail
PROJECT_DIR="${1:-victoriametrics-demo}"
VM_PORT="${VM_PORT:-18428}"            # 18428 to avoid colliding with a host VM
VM_CONTAINER="${VM_CONTAINER:-victoriametrics-demo-server}"

# --- 1. Bring up VictoriaMetrics ---
echo ">>> Starting VictoriaMetrics in Docker (container: $VM_CONTAINER)"
docker rm -f "$VM_CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$VM_CONTAINER" \
  -p "$VM_PORT:8428" \
  victoriametrics/victoria-metrics:latest \
  -storageDataPath=/tmp/vm-storage \
  -retentionPeriod=1d \
  >/dev/null

echo ">>> Waiting for VictoriaMetrics to come up..."
for i in $(seq 1 30); do
  # /-/ready returns "VictoriaMetrics is Ready"
  if curl -sS "http://localhost:$VM_PORT/-/ready" 2>/dev/null | grep -qi "Ready"; then
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

uv add -q 'yarl<1.24' pandas requests
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing components (--refresh on first call busts the registry-manifest cache)"
$CLI --refresh add synthetic_data_generator --auto-install
$CLI add victoriametrics_resource --auto-install
$CLI add dataframe_to_victoriametrics --auto-install
$CLI add victoriametrics_query_asset --auto-install

echo ">>> Overwriting CLI-installed example defs.yamls with demo-specific config"

# synthetic_data_generator → produce IoT sensor time-series.
# The `sensors` schema emits: sensor_id, timestamp, sensor_type, location,
# value, unit, status. timestamp + value get used as-is; the rest become
# labels on the VictoriaMetrics series.
cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: sensor_readings
  schema_type: sensors
  row_count: 10000
  group_name: source
EOF

# dataframe_to_victoriametrics → ingest into VM as the `sensor_reading` metric.
cat > "src/$PKG/defs/dataframe_to_victoriametrics/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_victoriametrics.component.DataframeToVictoriaMetricsComponent
attributes:
  asset_name: vm_sensor_ingest
  upstream_asset_key: sensor_readings
  metric_name: sensor_reading
  base_url_env_var: VM_BASE_URL
  timestamp_column: timestamp
  value_column: value
  label_columns: [sensor_id, sensor_type, location, unit, status]
  group_name: victoriametrics
EOF

# victoriametrics_query_asset → aggregate by sensor_type via PromQL.
cat > "src/$PKG/defs/victoriametrics_query_asset/defs.yaml" <<EOF
type: $PKG.components.victoriametrics_query_asset.component.VictoriaMetricsQueryAssetComponent
attributes:
  asset_name: vm_avg_by_sensor_type
  query: 'avg(sensor_reading) by (sensor_type)'
  query_range: true
  range_lookback_minutes: 1440
  step_seconds: 600
  base_url_env_var: VM_BASE_URL
  group_name: victoriametrics
  deps:
    - vm_sensor_ingest
EOF

# victoriametrics_resource → connection (optional — the dataframe sink + query
# asset use env-var-only config above; resource is here for any future demo
# expansion that uses the shared connection).
cat > "src/$PKG/defs/victoriametrics_resource/defs.yaml" <<EOF
type: $PKG.components.victoriametrics_resource.component.VictoriaMetricsResourceComponent
attributes:
  resource_key: vm_resource
  base_url: http://localhost:$VM_PORT
EOF

# --- 3. Final instructions ---
echo ""
echo "============================================================"
echo "VictoriaMetrics demo scaffolded at $PROJECT_DIR"
echo "============================================================"
echo ""
echo "VictoriaMetrics is running on:"
echo "  http://localhost:$VM_PORT"
echo ""
echo "Quick check:"
echo "  curl 'http://localhost:$VM_PORT/api/v1/labels'"
echo ""
echo "Next steps:"
echo ""
echo "  cd $PROJECT_DIR"
echo "  export VM_BASE_URL=http://localhost:$VM_PORT"
echo ""
echo "  uv run dg dev                                              # UI at http://localhost:3000"
echo "  # OR headless smoke test:"
echo "  uv run dg launch --assets '*'"
echo ""
echo "Then verify ingest worked (range query — instant query misses because"
echo "synthetic timestamps are distributed across 24h, none exactly at 'now'):"
echo "  NOW=\$(date +%s); BACK=\$((NOW - 86400))"
echo "  curl \"http://localhost:$VM_PORT/api/v1/query_range?query=count(sensor_reading)&start=\${BACK}&end=\${NOW}&step=600\""
echo ""
echo "Cleanup:"
echo "  docker rm -f $VM_CONTAINER"
echo ""
