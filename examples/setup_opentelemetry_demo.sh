#!/usr/bin/env bash
# OpenTelemetry full-stack demo — metrics + logs + traces in one pipeline.
#
# WHAT THIS DEMONSTRATES
#   ONE OTLP endpoint, three signals: metrics, logs, traces. Push synthetic
#   pipeline data through all three OTel sinks and watch them land in any
#   OTel-compatible backend (Honeycomb, Lightstep, Datadog, Jaeger, Tempo,
#   Grafana Cloud, etc.). This demo uses a local OTel collector container
#   that just logs received data to stdout — easy to verify locally; swap
#   the OTLP_ENDPOINT to point at any vendor.
#
# Pipeline (4 components):
#   synthetic_data_generator → orders_raw  ──┬─→ dataframe_to_otlp_metrics
#                                            ├─→ dataframe_to_otlp_logs
#                                            └─→ dataframe_to_otlp_traces
#
# PREREQS
#   1. Docker (for the local OTel collector)
#   2. uv / uvx
#
# REQUIRED ENV VARS
#   OTLP_ENDPOINT      base URL of an OTLP/HTTP receiver (default: http://localhost:4318)
#
# COST
#   $0 — local Docker collector
#
# TEARDOWN
#   docker rm -f dg-otel-demo

set -euo pipefail
PROJECT_DIR="${1:-opentelemetry-demo}"
OTLP_ENDPOINT="${OTLP_ENDPOINT:-http://localhost:4318}"

# ── 1. Start a local OTel collector if not already running ────────────────
if ! docker ps --format '{{.Names}}' | grep -q '^dg-otel-demo$'; then
  echo ">>> Starting local OTel collector (debug exporter — logs everything to stdout)"
  mkdir -p /tmp/otel-demo-config
  cat > /tmp/otel-demo-config/config.yml <<'EOF'
receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318
processors:
  batch:
exporters:
  debug:
    verbosity: detailed
service:
  pipelines:
    traces:  {receivers: [otlp], processors: [batch], exporters: [debug]}
    metrics: {receivers: [otlp], processors: [batch], exporters: [debug]}
    logs:    {receivers: [otlp], processors: [batch], exporters: [debug]}
EOF
  docker run -d --name dg-otel-demo -p 4318:4318 \
    -v /tmp/otel-demo-config/config.yml:/etc/otelcol-contrib/config.yaml \
    otel/opentelemetry-collector-contrib:latest \
    --config /etc/otelcol-contrib/config.yaml >/dev/null
  sleep 5
fi
echo ">>> OTel collector running on http://localhost:4318"

# ── 2. Scaffold Dagster project ───────────────────────────────────────────
echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas opentelemetry-sdk opentelemetry-exporter-otlp-proto-http
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 4 community components"
$CLI add synthetic_data_generator    --auto-install
$CLI add dataframe_to_otlp_metrics   --auto-install
$CLI add dataframe_to_otlp_logs      --auto-install
$CLI add dataframe_to_otlp_traces    --auto-install

echo ">>> Writing demo defs.yaml"

cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: orders_raw
  schema_type: orders
  row_count: 30
  random_state: 42
  description: 30 synthetic e-commerce orders
  group_name: ingest
EOF

cat > "src/$PKG/defs/dataframe_to_otlp_metrics/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_otlp_metrics.component.DataframeToOtlpMetricsComponent
attributes:
  asset_name: orders_metrics_otlp
  upstream_asset_key: orders_raw
  endpoint: $OTLP_ENDPOINT
  metric_name: orders.total
  metric_kind: sum                # counter — only goes up
  metric_unit: "_count"
  value_column: total              # numeric column on each order
  attribute_columns: [category, status]
  service_name: dagster_demo
  group_name: observability
EOF

cat > "src/$PKG/defs/dataframe_to_otlp_logs/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_otlp_logs.component.DataframeToOtlpLogsComponent
attributes:
  asset_name: orders_logs_otlp
  upstream_asset_key: orders_raw
  endpoint: $OTLP_ENDPOINT
  service_name: dagster_demo
  body_column: order_id            # log body = order ID
  severity_column: status          # 'shipped'/'pending' becomes severity (will fall back to INFO)
  attribute_columns: [customer_id, category, total]
  group_name: observability
EOF

cat > "src/$PKG/defs/dataframe_to_otlp_traces/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_otlp_traces.component.DataframeToOtlpTracesComponent
attributes:
  asset_name: orders_traces_otlp
  upstream_asset_key: orders_raw
  endpoint: $OTLP_ENDPOINT
  service_name: dagster_demo
  span_name_column: order_id
  trace_id_column: customer_id     # one trace per customer (groups orders into a customer journey)
  attribute_columns: [category, status, total]
  group_name: observability
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Verify (collector logs all received OTLP signals):
    docker logs dg-otel-demo 2>&1 | tail -100

Look for:
    - 'Name: orders.total' (the metric)
    - 'Body: Str(ORD00000001)' or similar (logs)
    - 'Name           : ORD00000001' (spans)

Swap to a real backend:
    OTLP_ENDPOINT=https://api.honeycomb.io
    # plus set bearer_token_env_var: HONEYCOMB_API_KEY in each defs.yaml

Teardown:
    docker rm -f dg-otel-demo
MSG
