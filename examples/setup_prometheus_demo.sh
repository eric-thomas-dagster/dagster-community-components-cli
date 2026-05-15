#!/usr/bin/env bash
# Prometheus full demo — sink + source + change detection.
#
# WHAT THIS DEMONSTRATES
#   30 synthetic orders (wide DataFrame: timestamp/region/category/value)
#   → unpivot to long form → push as Prometheus gauge metrics via
#   pushgateway → query back via PromQL → CSV report.
#
# Pipeline (5 components):
#   synthetic_data_generator → unpivot → dataframe_to_prometheus
#                                              ↓
#                                        Prometheus (scrapes pushgateway)
#                                              ↓
#                                  dataframe_from_prometheus → dataframe_to_csv
#
# PREREQS
#   1. Docker (for local Prometheus + pushgateway)
#   2. uv / uvx
#
# COST
#   $0 — local Docker
#
# TEARDOWN
#   docker rm -f dg-prom dg-pgw

set -euo pipefail
PROJECT_DIR="${1:-prometheus-demo}"
PUSHGATEWAY_URL="${PUSHGATEWAY_URL:-http://localhost:9091}"
PROMETHEUS_URL="${PROMETHEUS_URL:-http://localhost:9090}"

# 1. Start local Prometheus + pushgateway
if ! docker ps --format '{{.Names}}' | grep -q '^dg-pgw$'; then
  docker run -d --name dg-pgw -p 9091:9091 prom/pushgateway:latest >/dev/null
fi
if ! docker ps --format '{{.Names}}' | grep -q '^dg-prom$'; then
  mkdir -p /tmp/prom-demo-config
  cat > /tmp/prom-demo-config/prometheus.yml <<'EOF'
global:
  scrape_interval: 5s
scrape_configs:
  - job_name: pushgateway
    honor_labels: true
    static_configs:
      - targets: ['host.docker.internal:9091']
EOF
  docker run -d --name dg-prom -p 9090:9090 \
    -v /tmp/prom-demo-config/prometheus.yml:/etc/prometheus/prometheus.yml \
    prom/prometheus:latest >/dev/null
  sleep 5
fi
echo ">>> Pushgateway: $PUSHGATEWAY_URL  Prometheus: $PROMETHEUS_URL"

# 2. Scaffold + install
echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas prometheus_client requests
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 7 community components"
$CLI add synthetic_data_generator   --auto-install
$CLI add unpivot                    --auto-install
$CLI add prometheus_resource        --auto-install
$CLI add dataframe_to_prometheus    --auto-install
$CLI add dataframe_from_prometheus  --auto-install
$CLI add dataframe_to_csv           --auto-install
# local_parquet_io_manager persists every DataFrame asset to disk so the
# multiprocess executor's subprocesses can share state across steps.
# Without it, the default in-memory IO manager loses outputs between steps.
$CLI add local_parquet_io_manager   --auto-install

echo ">>> Writing demo defs.yaml"

cat > "src/$PKG/defs/local_parquet_io_manager/defs.yaml" <<EOF
type: $PKG.components.local_parquet_io_manager.component.LocalParquetIOManagerComponent
attributes:
  resource_key: io_manager
  base_dir: /tmp/prometheus-demo-storage
  create_dir: true
EOF

cat > "src/$PKG/defs/prometheus_resource/defs.yaml" <<EOF
type: $PKG.components.prometheus_resource.component.PrometheusResourceComponent
attributes:
  gateway: $PUSHGATEWAY_URL
  timeout: 30
  resource_key: prometheus
EOF

cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: orders_raw
  schema_type: orders
  row_count: 30
  random_state: 42
  group_name: ingest
EOF

cat > "src/$PKG/defs/unpivot/defs.yaml" <<EOF
type: $PKG.components.unpivot.component.UnpivotComponent
attributes:
  asset_name: orders_long
  upstream_asset_key: orders_raw
  id_columns: [order_id, customer_id, category]
  value_columns: [total, num_items]
  var_name: metric
  value_name: value
  group_name: transform
EOF

cat > "src/$PKG/defs/dataframe_to_prometheus/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_prometheus.component.DataframeToPrometheusComponent
attributes:
  asset_name: orders_metrics_pushed
  upstream_asset_key: orders_long
  pushgateway_url: $PUSHGATEWAY_URL
  job: dagster_orders_demo
  metric_name: orders_metric
  metric_type: gauge
  metric_help: "Order metrics by category and metric type"
  value_column: value
  label_columns: [category, metric]
  group_name: push
EOF

cat > "src/$PKG/defs/dataframe_from_prometheus/defs.yaml" <<EOF
type: $PKG.components.dataframe_from_prometheus.component.DataframeFromPrometheusComponent
attributes:
  asset_name: revenue_by_category
  server_url: $PROMETHEUS_URL
  query: 'sum by (category) (orders_metric{job="dagster_orders_demo", metric="total"})'
  range_query: false
  deps: [orders_metrics_pushed]
  group_name: query
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: revenue_report
  upstream_asset_key: revenue_by_category
  file_path: /tmp/prometheus_revenue_by_category.csv
  group_name: report
EOF

cat <<MSG

>>> Setup complete.

NOTE: Set DAGSTER_HOME so the multi-step launches share IO-manager storage.
Without it, each subprocess gets its own ephemeral tmp dir and downstream
steps can't load upstream outputs.

Materialize (sleep 8s after first run for Prometheus to scrape pushgateway):
    cd $PROJECT_DIR
    # Note: '*X' selects X + ALL upstreams. '+X' is just one level up — not
    # enough for chains like orders_raw → orders_long → orders_metrics_pushed.
    uv run dg launch --assets '*orders_metrics_pushed'    # push (full chain)
    sleep 8                                                # let scraper run
    uv run dg launch --assets '*revenue_report'            # query + report

Verify:
    head /tmp/prometheus_revenue_by_category.csv
    curl -s '$PROMETHEUS_URL/api/v1/query?query=orders_metric' | head

Teardown:
    docker rm -f dg-prom dg-pgw
MSG
