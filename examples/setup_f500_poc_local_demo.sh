#!/usr/bin/env bash
# Fortune 500 POC end-to-end demo — local, no credentials.
#
# WHAT THIS DEMONSTRATES (end-to-end, on your laptop)
#   6-container infra + 3 real Dagster code-locations + cross-domain deps:
#     - Postgres (legacy source DBs for sales / marketing / finance)
#     - MinIO (S3-compatible landing zone, default IO manager for all 3 projects)
#     - Trino (federated SQL query engine for cross-schema reads)
#     - DuckDB (warehouse layer, file-based)
#     - Elasticsearch + Kibana (central log platform)
#     - OpenTelemetry Collector (receives Dagster compute logs → ES)
#
#   Dagster workspace with 3 code-locations (real cross-code-location deps):
#     - sales/       Data Vault 2.0 (hub / link / sat) + warehouse write
#     - marketing/   raw ingest + attribution asset depending on sales/customer_hub
#     - finance/     raw GL + Trino federated P&L + cross-domain freshness check
#                    + shell_command_asset + k8s_job_asset stub
#
#   OtlpComputeLogManager wired in dagster.yaml → op stdout/stderr lands in ES.
#
# COST: $0. Pulls ~2GB across 6 containers on first run.
# TIME: ~10 min first run, ~2 min thereafter.
#
# USAGE: bash setup_f500_poc_local_demo.sh [workspace_dir]

set -eo pipefail

WORKSPACE_DIR="${1:-f500-poc-demo}"
POSTGRES_PORT="${POSTGRES_PORT:-15432}"
MINIO_PORT="${MINIO_PORT:-19000}"
MINIO_CONSOLE_PORT="${MINIO_CONSOLE_PORT:-19001}"
TRINO_PORT="${TRINO_PORT:-18080}"
ES_PORT="${ES_PORT:-19200}"
KIBANA_PORT="${KIBANA_PORT:-15601}"
OTEL_HTTP_PORT="${OTEL_HTTP_PORT:-14318}"
COMMIT_SHA="${COMMIT_SHA:-main}"    # bump after push

if ! command -v docker >/dev/null 2>&1; then echo "✗ docker required"; exit 1; fi
if ! command -v uv >/dev/null 2>&1; then echo "✗ uv required (https://docs.astral.sh/uv/)"; exit 1; fi

# --- 1. Fresh workspace dir ------------------------------------------------
rm -rf "$WORKSPACE_DIR"
mkdir -p "$WORKSPACE_DIR"
WORKSPACE_ABS="$(cd "$WORKSPACE_DIR" && pwd)"
cd "$WORKSPACE_ABS"

# --- 2. Infra: docker-compose ---------------------------------------------
mkdir -p infra/trino-catalog infra/otel-collector warehouse

cat > infra/docker-compose.yml <<COMPOSEEOF
name: f500-poc

services:
  postgres:
    image: postgres:16-alpine
    container_name: f500-postgres
    environment:
      POSTGRES_USER: f500
      POSTGRES_PASSWORD: f500pass
      POSTGRES_DB: legacy
    ports: ["${POSTGRES_PORT}:5432"]
    volumes:
      - ./init.sql:/docker-entrypoint-initdb.d/init.sql
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U f500"]
      interval: 2s
      timeout: 5s
      retries: 20

  minio:
    image: minio/minio:latest
    container_name: f500-minio
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: minioadmin
      MINIO_ROOT_PASSWORD: minioadmin
    ports:
      - "${MINIO_PORT}:9000"
      - "${MINIO_CONSOLE_PORT}:9001"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9000/minio/health/live"]
      interval: 2s
      timeout: 5s
      retries: 20

  minio-init:
    image: minio/mc:latest
    container_name: f500-minio-init
    depends_on:
      minio:
        condition: service_healthy
    entrypoint: >
      /bin/sh -c "
      mc alias set local http://minio:9000 minioadmin minioadmin;
      mc mb -p local/lake || true;
      mc anonymous set download local/lake || true;
      echo '✓ MinIO bucket lake created';
      "

  trino:
    image: trinodb/trino:latest
    container_name: f500-trino
    ports: ["${TRINO_PORT}:8080"]
    volumes:
      - ./trino-catalog:/etc/trino/catalog
    depends_on:
      postgres:
        condition: service_healthy

  elasticsearch:
    image: docker.elastic.co/elasticsearch/elasticsearch:8.15.0
    container_name: f500-elasticsearch
    environment:
      - discovery.type=single-node
      - xpack.security.enabled=false
      - "ES_JAVA_OPTS=-Xms512m -Xmx512m"
    ports: ["${ES_PORT}:9200"]
    healthcheck:
      test: ["CMD-SHELL", "curl -sf http://localhost:9200/_cluster/health >/dev/null || exit 1"]
      interval: 5s
      timeout: 10s
      retries: 30

  kibana:
    image: docker.elastic.co/kibana/kibana:8.15.0
    container_name: f500-kibana
    environment:
      - ELASTICSEARCH_HOSTS=http://elasticsearch:9200
    ports: ["${KIBANA_PORT}:5601"]
    depends_on:
      elasticsearch:
        condition: service_healthy

  otel-collector:
    image: otel/opentelemetry-collector-contrib:latest
    container_name: f500-otel-collector
    command: ["--config=/etc/otel-collector-config.yaml"]
    volumes:
      - ./otel-collector-config.yaml:/etc/otel-collector-config.yaml
    ports:
      - "${OTEL_HTTP_PORT}:4318"
    depends_on:
      elasticsearch:
        condition: service_healthy
COMPOSEEOF

# --- 3. Postgres seed data (3 domains) ------------------------------------
cat > infra/init.sql <<'SQLEOF'
CREATE SCHEMA sales;
CREATE TABLE sales.customers (
    customer_id INT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT,
    phone TEXT,
    address TEXT,
    updated_at TIMESTAMP DEFAULT now()
);
CREATE TABLE sales.orders (
    order_id INT PRIMARY KEY,
    customer_id INT NOT NULL REFERENCES sales.customers(customer_id),
    order_date DATE NOT NULL,
    order_amount NUMERIC(12,2),
    order_status TEXT,
    updated_at TIMESTAMP DEFAULT now()
);
INSERT INTO sales.customers VALUES
  (1, 'Acme Corp',    'contact@acme.com',    '555-0001', '1 Acme Way',    now()),
  (2, 'Globex Inc',   'sales@globex.com',    '555-0002', '2 Globex Blvd', now()),
  (3, 'Umbrella LLC', 'orders@umbrella.co',  '555-0003', '3 Rain St',     now());
INSERT INTO sales.orders VALUES
  (100, 1, '2026-07-01', 1250.00, 'delivered',   now()),
  (101, 2, '2026-07-05', 4200.50, 'shipped',     now()),
  (102, 1, '2026-07-08',  875.75, 'processing',  now()),
  (103, 3, '2026-07-09', 3100.00, 'shipped',     now());

CREATE SCHEMA marketing;
CREATE TABLE marketing.campaigns (
    campaign_id INT PRIMARY KEY,
    name TEXT NOT NULL,
    channel TEXT,
    start_date DATE,
    end_date DATE,
    spend_usd NUMERIC(12,2)
);
CREATE TABLE marketing.campaign_touches (
    touch_id BIGSERIAL PRIMARY KEY,
    campaign_id INT NOT NULL REFERENCES marketing.campaigns(campaign_id),
    customer_id INT NOT NULL,
    touch_date TIMESTAMP DEFAULT now()
);
INSERT INTO marketing.campaigns VALUES
  (10, 'Q3 Retention',    'email',    '2026-07-01', '2026-07-31',  5000.00),
  (11, 'Enterprise Push', 'linkedin', '2026-07-15', '2026-08-15', 12000.00);
INSERT INTO marketing.campaign_touches (campaign_id, customer_id) VALUES
  (10, 1), (10, 2), (11, 3), (11, 1);

CREATE SCHEMA finance;
CREATE TABLE finance.gl_entries (
    entry_id INT PRIMARY KEY,
    account TEXT NOT NULL,
    debit  NUMERIC(14,2),
    credit NUMERIC(14,2),
    period_year INT,
    period_month INT
);
INSERT INTO finance.gl_entries VALUES
  (1, 'Revenue',  0,        9426.25, 2026, 7),
  (2, 'COGS',     3200.00,  0,       2026, 7),
  (3, 'OpEx',     5000.00,  0,       2026, 7),
  (4, 'MktSpend', 17000.00, 0,       2026, 7);
SQLEOF

# --- 4. Trino catalog: read from Postgres ----------------------------------
cat > infra/trino-catalog/postgres.properties <<TRINOCAT
connector.name=postgresql
connection-url=jdbc:postgresql://postgres:5432/legacy
connection-user=f500
connection-password=f500pass
TRINOCAT

# --- 5. OTel Collector: OTLP HTTP → Elasticsearch --------------------------
cat > infra/otel-collector-config.yaml <<'OTELEOF'
receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318

processors:
  batch:
    timeout: 5s

exporters:
  elasticsearch:
    endpoints:
      - http://elasticsearch:9200
    # Backed by a data stream (created in the setup script) — the exporter's
    # default _bulk `create` action requires this shape on ES 8.x.
    logs_index: dagster-compute-logs
  debug:
    verbosity: basic

service:
  pipelines:
    logs:
      receivers: [otlp]
      processors: [batch]
      exporters: [elasticsearch, debug]
OTELEOF

# --- 6. Start the stack ----------------------------------------------------
echo ">>> Starting F500 POC infrastructure (6 containers)"
docker compose -f infra/docker-compose.yml up -d --wait 2>&1 | tail -10 || true

for _ in $(seq 1 30); do
  if docker exec f500-postgres pg_isready -U f500 >/dev/null 2>&1; then
    echo "    ✓ Postgres ready on :$POSTGRES_PORT"; break
  fi
  sleep 1
done
echo "    ✓ MinIO on :$MINIO_PORT   (console http://localhost:$MINIO_CONSOLE_PORT — minioadmin/minioadmin)"
echo "    ✓ Trino on :$TRINO_PORT"
# Enable auto-create at cluster level, then register an index template + data stream
# for dagster-compute-logs. The OTel ES exporter defaults to _bulk `create` action
# (data-stream mode), so we back it with an actual data stream.
curl -sf -X PUT "http://localhost:${ES_PORT}/_cluster/settings" \
  -H "Content-Type: application/json" \
  -d '{"persistent":{"action.auto_create_index":"true"}}' >/dev/null 2>&1 || true
curl -sf -X PUT "http://localhost:${ES_PORT}/_index_template/dagster-compute-logs" \
  -H "Content-Type: application/json" \
  -d '{
    "index_patterns": ["dagster-compute-logs*"],
    "data_stream": {},
    "priority": 500,
    "template": {"mappings": {"properties": {"@timestamp": {"type": "date"}}}}
  }' >/dev/null 2>&1 || true
curl -sf -X PUT "http://localhost:${ES_PORT}/_data_stream/dagster-compute-logs" >/dev/null 2>&1 || true
echo "    ✓ Elasticsearch on :$ES_PORT (dagster-compute-logs data stream registered)"
echo "    ✓ Kibana on http://localhost:$KIBANA_PORT"
echo "    ✓ OTel Collector OTLP/HTTP on :$OTEL_HTTP_PORT"

# --- 7. Scaffold Dagster workspace -----------------------------------------
echo ">>> Scaffolding Dagster workspace at $WORKSPACE_ABS"
uvx create-dagster@latest workspace . --no-uv-sync 2>&1 | tail -3 || true

# --- 8. Install shared deps into deployment venv ---------------------------
# For local dev / testing: `export DCC_LOCAL_PATH=/path/to/dagster-component-templates-src`
# to install the DCC package from a local checkout instead of GitHub.
if [ -n "$DCC_LOCAL_PATH" ]; then
  DCC_SRC="dagster-community-components @ file://$DCC_LOCAL_PATH"
  echo "    (using local DCC checkout: $DCC_LOCAL_PATH)"
else
  DCC_SRC="dagster-community-components @ https://github.com/eric-thomas-dagster/dagster-component-templates/archive/$COMMIT_SHA.zip"
fi

cd deployments/local
uv add -q \
  dagster-dg-cli \
  dagster-webserver \
  "$DCC_SRC" \
  pandas \
  duckdb \
  pyarrow \
  s3fs \
  trino \
  psycopg2-binary \
  sqlalchemy \
  requests
cd "$WORKSPACE_ABS"

# --- 9. Create 3 code-location projects + install per-project deps ---------
# Each code-location runs its own subprocess with its own venv — dg's model.
# Total install time: ~1-2 min per project (uv is fast + cached across projects).
echo ">>> Creating 3 code-location projects (sales, marketing, finance)"
COMMON_DEPS=(pandas duckdb pyarrow s3fs trino psycopg2-binary sqlalchemy requests deltalake)
FINANCE_EXTRA_DEPS=(dagster-k8s)   # for K8sJobAssetComponent stub (ShellCommandAssetComponent uses stdlib subprocess)
for domain in sales marketing finance; do
  uvx create-dagster@latest project "projects/$domain" --no-uv-sync 2>&1 | tail -2 || true
  (
    cd "projects/$domain"
    if [ "$domain" = "finance" ]; then
      uv add -q "$DCC_SRC" "${COMMON_DEPS[@]}" "${FINANCE_EXTRA_DEPS[@]}"
    else
      uv add -q "$DCC_SRC" "${COMMON_DEPS[@]}"
    fi
  )
done

# --- 10. Env vars (exported BEFORE yaml writes so $PG_DSN gets bash-expanded) --
export PG_DSN="postgresql://f500:f500pass@localhost:${POSTGRES_PORT}/legacy"
export MINIO_ACCESS_KEY=minioadmin
export MINIO_SECRET_KEY=minioadmin
export DAGSTER_HOME="$WORKSPACE_ABS"

# --- 11. Write defs.yaml files directly (100% components, no vendored code) --
# DCC is installed as a package in each project's venv, so `type:` can reference
# `dagster_community_components.<X>Component` directly. dg's autoloader picks up
# every `defs.yaml` under each project's src/<pkg>/defs/**/ tree.
# $PG_DSN is expanded at write time (bash heredoc); MinIO env vars stay as var
# names (the component's *_env_var fields read them at runtime).
echo ">>> Writing defs.yaml files for each code-location"

SALES_PKG="$(ls projects/sales/src/ | head -1)"
MKT_PKG="$(ls projects/marketing/src/ | head -1)"
FIN_PKG="$(ls projects/finance/src/ | head -1)"
SALES_DEFS="projects/sales/src/$SALES_PKG/defs"
MKT_DEFS="projects/marketing/src/$MKT_PKG/defs"
FIN_DEFS="projects/finance/src/$FIN_PKG/defs"

write_defs() {
  # write_defs <path/to/defs.yaml> <heredoc-body>
  local path="$1"; shift
  mkdir -p "$(dirname "$path")"
  cat > "$path"
}

# --- Sales code-location ---
write_defs "$SALES_DEFS/minio_io_manager/defs.yaml" <<YAML
type: dagster_community_components.MinIOIOManagerComponent
attributes:
  resource_key: io_manager
  endpoint_url: http://localhost:${MINIO_PORT}
  access_key_env_var: MINIO_ACCESS_KEY
  secret_key_env_var: MINIO_SECRET_KEY
  bucket: lake
  prefix: sales
YAML

write_defs "$SALES_DEFS/raw_customers/defs.yaml" <<YAML
type: dagster_community_components.DatabaseQueryComponent
attributes:
  asset_name: raw_customers
  group_name: sales_raw
  database_url: $PG_DSN
  query: |
    SELECT customer_id, name, email, phone, address, updated_at
    FROM sales.customers
YAML

write_defs "$SALES_DEFS/raw_orders/defs.yaml" <<YAML
type: dagster_community_components.DatabaseQueryComponent
attributes:
  asset_name: raw_orders
  group_name: sales_raw
  database_url: $PG_DSN
  query: |
    SELECT order_id, customer_id, order_date, order_amount, order_status, updated_at
    FROM sales.orders
YAML

write_defs "$SALES_DEFS/customer_dv2/defs.yaml" <<YAML
type: dagster_community_components.DataVaultHubLinkSatelliteComponent
attributes:
  entity: customer
  upstream_asset_key: raw_customers
  business_keys: [customer_id]
  satellite_columns: [name, email, phone, address, updated_at]
  record_source: sales_erp
  group_name: sales_dv2
  asset_key_prefix: [sales, dv2]
YAML

write_defs "$SALES_DEFS/order_dv2/defs.yaml" <<YAML
type: dagster_community_components.DataVaultHubLinkSatelliteComponent
attributes:
  entity: order
  upstream_asset_key: raw_orders
  business_keys: [order_id]
  link_business_keys: [customer_id, order_id]
  satellite_columns: [order_date, order_amount, order_status, updated_at]
  record_source: sales_erp
  group_name: sales_dv2
  asset_key_prefix: [sales, dv2]
YAML

write_defs "$SALES_DEFS/warehouse_dim_customer/defs.yaml" <<YAML
type: dagster_community_components.DuckDBTableWriterComponent
attributes:
  asset_name: warehouse_dim_customer
  upstream_asset_key: sales/dv2/customer_sat
  database_path: ${WORKSPACE_ABS}/warehouse/f500.duckdb
  table: sales_dim_customer
  write_mode: replace
  group_name: sales_warehouse
YAML

# --- Marketing code-location ---
write_defs "$MKT_DEFS/minio_io_manager/defs.yaml" <<YAML
type: dagster_community_components.MinIOIOManagerComponent
attributes:
  resource_key: io_manager
  endpoint_url: http://localhost:${MINIO_PORT}
  access_key_env_var: MINIO_ACCESS_KEY
  secret_key_env_var: MINIO_SECRET_KEY
  bucket: lake
  prefix: marketing
YAML

write_defs "$MKT_DEFS/raw_campaigns/defs.yaml" <<YAML
type: dagster_community_components.DatabaseQueryComponent
attributes:
  asset_name: raw_campaigns
  group_name: marketing_raw
  database_url: $PG_DSN
  query: |
    SELECT campaign_id, name, channel, start_date, end_date, spend_usd
    FROM marketing.campaigns
YAML

write_defs "$MKT_DEFS/raw_touches/defs.yaml" <<YAML
type: dagster_community_components.DatabaseQueryComponent
attributes:
  asset_name: raw_touches
  group_name: marketing_raw
  database_url: $PG_DSN
  query: |
    SELECT touch_id, campaign_id, customer_id, touch_date
    FROM marketing.campaign_touches
YAML

# Cross-domain: marketing attribution depends on sales/dv2/customer_hub.
# Dagster resolves the dep across code-locations by AssetKey.
write_defs "$MKT_DEFS/campaign_attribution/defs.yaml" <<YAML
type: dagster_community_components.DatabaseQueryComponent
attributes:
  asset_name: campaign_attribution
  group_name: marketing_analytics
  database_url: $PG_DSN
  deps:
    - sales/dv2/customer_hub
    - raw_campaigns
    - raw_touches
  query: |
    SELECT
      c.campaign_id,
      c.name AS campaign_name,
      COUNT(DISTINCT t.customer_id) AS attributed_customers,
      c.spend_usd
    FROM marketing.campaigns c
    LEFT JOIN marketing.campaign_touches t USING (campaign_id)
    GROUP BY c.campaign_id, c.name, c.spend_usd
YAML

write_defs "$MKT_DEFS/warehouse_attribution/defs.yaml" <<YAML
type: dagster_community_components.DuckDBTableWriterComponent
attributes:
  asset_name: warehouse_attribution
  upstream_asset_key: campaign_attribution
  database_path: ${WORKSPACE_ABS}/warehouse/f500.duckdb
  table: marketing_attribution
  write_mode: replace
  group_name: marketing_warehouse
YAML

# --- Finance code-location ---
write_defs "$FIN_DEFS/minio_io_manager/defs.yaml" <<YAML
type: dagster_community_components.MinIOIOManagerComponent
attributes:
  resource_key: io_manager
  endpoint_url: http://localhost:${MINIO_PORT}
  access_key_env_var: MINIO_ACCESS_KEY
  secret_key_env_var: MINIO_SECRET_KEY
  bucket: lake
  prefix: finance
YAML

write_defs "$FIN_DEFS/raw_gl_entries/defs.yaml" <<YAML
type: dagster_community_components.DatabaseQueryComponent
attributes:
  asset_name: raw_gl_entries
  group_name: finance_raw
  database_url: $PG_DSN
  query: |
    SELECT entry_id, account, debit, credit, period_year, period_month
    FROM finance.gl_entries
YAML

# Trino federated: joins postgres.finance with postgres.sales via one query.
write_defs "$FIN_DEFS/federated_pnl/defs.yaml" <<YAML
type: dagster_community_components.TrinoQueryComponent
attributes:
  asset_name: federated_pnl
  host: localhost
  port: ${TRINO_PORT}
  user: f500
  catalog: postgres
  schema_name: finance
  query: |
    SELECT
      g.account,
      SUM(g.credit - g.debit) AS net_amount,
      (SELECT COUNT(*) FROM postgres.sales.orders) AS sales_order_count
    FROM postgres.finance.gl_entries g
    GROUP BY g.account
  group_name: finance_federated
  deps:
    - raw_gl_entries
    - sales/dv2/order_hub
YAML

write_defs "$FIN_DEFS/warehouse_pnl/defs.yaml" <<YAML
type: dagster_community_components.DuckDBTableWriterComponent
attributes:
  asset_name: warehouse_pnl
  upstream_asset_key: federated_pnl
  database_path: ${WORKSPACE_ABS}/warehouse/f500.duckdb
  table: finance_pnl
  write_mode: replace
  group_name: finance_warehouse
YAML

# Cross-domain freshness check: finance blocks on sales/dv2/customer_sat freshness.
write_defs "$FIN_DEFS/cross_domain_check/defs.yaml" <<YAML
type: dagster_community_components.FreshnessPolicyComponent
attributes:
  asset_key: sales/dv2/customer_sat
  policy_type: time_window
  fail_window_hours: 24
  warn_window_hours: 12
YAML

write_defs "$FIN_DEFS/legacy_nightly_prep/defs.yaml" <<YAML
type: dagster_community_components.ShellCommandAssetComponent
attributes:
  asset_name: legacy_nightly_prep
  group_name: finance_legacy
  command: "echo '[legacy] nightly prep started at' \$(date -u +%FT%TZ); sleep 1; echo '[legacy] done'"
YAML

write_defs "$FIN_DEFS/dbt_on_gke/defs.yaml" <<YAML
type: dagster_community_components.K8sJobAssetComponent
attributes:
  asset_name: dbt_marts_on_gke
  group_name: finance_legacy
  image: ghcr.io/acme/dbt-bigquery:latest
  command: ["dbt", "run", "--target", "prod"]
  namespace: dagster
  cpu_limit: 2000m
  memory_limit: 4Gi
YAML

# --- 13. Workspace dagster.yaml — wire OtlpComputeLogManager --------------
mkdir -p storage/compute-logs
cat > dagster.yaml <<YAML
# OtlpComputeLogManager — op stdout/stderr → OTel Collector → Elasticsearch.
# Kibana at http://localhost:${KIBANA_PORT} → data views → dagster-compute-logs
compute_logs:
  module: dagster_community_components.compute_log_managers.otlp
  class: OtlpComputeLogManager
  config:
    otlp_endpoint: http://localhost:${OTEL_HTTP_PORT}
    service_name: dagster-f500-poc
    local_dir: ${WORKSPACE_ABS}/storage/compute-logs
    severity_stdout: 9
    severity_stderr: 13
    batch_size: 20
    upload_interval: 5
    skip_empty_files: false
YAML

# --- 14. Env vars — write .env for reproducibility (already exported above) --
cat > .env <<ENVEOF
PG_DSN=$PG_DSN
MINIO_ACCESS_KEY=$MINIO_ACCESS_KEY
MINIO_SECRET_KEY=$MINIO_SECRET_KEY
DAGSTER_HOME=$WORKSPACE_ABS
ENVEOF

# --- 15. Validate defs -----------------------------------------------------
# dg is run from the workspace root against the deployment env; discovers 3 code-locations from dg.toml.
echo ">>> Running dg check defs against 3 code-locations"
if ! uv run --project deployments/local dg check defs 2>&1 | tail -20; then
  echo "    ✗ dg check failed — inspect above"; exit 1
fi
echo "    ✓ dg check defs passed for all 3 code-locations"

# --- 16. Smoke-test materialization ---------------------------------------
# dg launch runs per project — cross-loc deps resolve via AssetKey. Order: sales → marketing → finance.
# Note: dbt_marts_on_gke is a k8s stub — validates via dg check, needs a real cluster to run.
# We exclude it from the smoke test but leave it in the graph for demo browsing.
SALES_ASSETS='*'
MKT_ASSETS='*'
FIN_ASSETS='raw_gl_entries,federated_pnl,warehouse_pnl,legacy_nightly_prep'   # skip dbt_marts_on_gke (needs k8s)
echo ">>> Smoke-test: materialize each code-location's assets in dependency order"
FAILED=0
for domain in sales marketing finance; do
  echo "    → materializing $domain assets..."
  case "$domain" in
    sales)     ASSETS="$SALES_ASSETS" ;;
    marketing) ASSETS="$MKT_ASSETS"   ;;
    finance)   ASSETS="$FIN_ASSETS"   ;;
  esac
  (
    cd "projects/$domain"
    PG_DSN="$PG_DSN" MINIO_ACCESS_KEY="$MINIO_ACCESS_KEY" MINIO_SECRET_KEY="$MINIO_SECRET_KEY" \
      DAGSTER_HOME="$DAGSTER_HOME" \
      uv run dg launch --assets "$ASSETS" 2>&1 | tail -12
  ) || FAILED=1
done
if [ "$FAILED" != "0" ]; then
  echo "    ⚠ one or more code-locations had materialization failures — inspect above"
else
  echo "    ✓ all code-locations materialized (dbt_marts_on_gke skipped — needs real k8s cluster)"
fi

# --- 17. Verify Elasticsearch received compute logs -----------------------
echo ">>> Waiting 15s for OTel batch to flush to Elasticsearch..."
sleep 15
LOG_COUNT="$(curl -s "http://localhost:${ES_PORT}/dagster-compute-logs*/_count" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("count", 0))' 2>/dev/null || echo 0)"
if [ "$LOG_COUNT" -gt 0 ]; then
  echo "    ✓ Elasticsearch has $LOG_COUNT compute log records — view in Kibana"
else
  echo "    ⚠ Elasticsearch has 0 compute log records — check OTel collector logs:"
  echo "        docker logs f500-otel-collector"
fi

# --- 18. Done --------------------------------------------------------------
cat <<DONE

✓ F500 POC demo up and running.

Infrastructure:
  Postgres         localhost:$POSTGRES_PORT       f500 / f500pass / legacy
  MinIO console    http://localhost:$MINIO_CONSOLE_PORT  minioadmin / minioadmin
  MinIO S3 API     http://localhost:$MINIO_PORT
  Trino            http://localhost:$TRINO_PORT
  Elasticsearch    http://localhost:$ES_PORT
  Kibana           http://localhost:$KIBANA_PORT   (data view: dagster-compute-logs*)
  OTel Collector   http://localhost:$OTEL_HTTP_PORT (OTLP/HTTP)

Dagster workspace: $WORKSPACE_ABS
  ├── dg.toml                    workspace manifest (3 code-locations)
  ├── dagster.yaml               OtlpComputeLogManager → OTel → ES
  ├── projects/sales/            code-location 1: DV2.0 hub/link/sat
  ├── projects/marketing/        code-location 2: attribution (cross-loc dep on sales)
  ├── projects/finance/          code-location 3: Trino federated + cross-domain check
  └── warehouse/f500.duckdb      persistent warehouse

Next:
  cd $WORKSPACE_ABS/deployments/local
  uv run dg dev
  # → http://localhost:3000
  # → all 3 code-locations show side by side; browse cross-loc lineage
  #   from marketing/campaign_attribution → sales/dv2/customer_hub

Cleanup:
  docker compose -f $WORKSPACE_ABS/infra/docker-compose.yml down -v
  rm -rf $WORKSPACE_ABS
DONE
