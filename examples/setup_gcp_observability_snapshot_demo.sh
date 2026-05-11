#!/usr/bin/env bash
# GCP Observability Snapshot — pull Cloud Logging entries + Cloud Monitoring
# metrics into BigQuery, where ops / SRE can run ad-hoc SQL across both.
#
# WHAT THIS DEMONSTRATES (validated live against servicepulse-490502)
#   Two new GCP source components, a pandas flatten step, and BQ sink:
#     cloud_logging_query_asset       → recent_errors  (Cloud Logging filter)
#     cloud_monitoring_metrics_asset  → api_call_metrics (Cloud Monitoring TS)
#     pandas (JSON-stringify dicts)   → flatten for BQ load
#     dataframe_to_bigquery (×2)      → both DataFrames land in BigQuery tables
#
# Asset graph:
#   recent_errors        ← cloud_logging_query_asset (severity>=ERROR, 24h)
#         └── errors_flat  ← pandas (stringify dict cols)
#                  └── errors_bq  ← dataframe_to_bigquery
#
#   api_call_metrics     ← cloud_monitoring_metrics_asset (api/request_count, 1h)
#         └── metrics_flat ← pandas (stringify dict cols)
#                  └── metrics_bq ← dataframe_to_bigquery
#
# WHY THE FLATTEN STEP?
#   Both source components produce nested dict columns (resource_labels,
#   metric_labels, json_payload, etc.). BigQuery's load_table_from_dataframe
#   can't infer a schema for object dtype dict/list columns. JSON-stringifying
#   them is the simplest portable fix.
#
# REQUIRED ENV VARS
#   GOOGLE_APPLICATION_CREDENTIALS  service-account JSON path
#   GCP_PROJECT_ID                  project id (e.g. servicepulse-490502)
#   BQ_DATASET                      destination BQ dataset (e.g. dagster_demo)
#
# REQUIRED APIS (enable URLs)
#   Cloud Logging      https://console.cloud.google.com/apis/library/logging.googleapis.com
#   Cloud Monitoring   https://console.cloud.google.com/apis/library/monitoring.googleapis.com
#   BigQuery           https://console.cloud.google.com/apis/library/bigquery.googleapis.com
#
# REQUIRED IAM (on the service account)
#   roles/logging.viewer
#   roles/monitoring.viewer
#   roles/bigquery.dataEditor      (on $BQ_DATASET)
#   roles/bigquery.jobUser         (project-level)
#
# COST while running
#   Free. Cloud Logging reads are free up to 50 GB/mo, Monitoring metric
#   reads are free, BQ loads here are kilobytes.

set -euo pipefail
PROJECT_DIR="${1:-gcp-observability-snapshot-demo}"

if [ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]; then
  echo "ERROR: set GOOGLE_APPLICATION_CREDENTIALS"; exit 1
fi
if [ -z "${GCP_PROJECT_ID:-}" ]; then
  echo "ERROR: set GCP_PROJECT_ID"; exit 1
fi
BQ_DATASET="${BQ_DATASET:-dagster_demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas google-auth google-cloud-logging google-cloud-monitoring google-cloud-bigquery db-dtypes
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing components"
$CLI add cloud_logging_query_asset           --auto-install 2>&1 | tail -2
$CLI add cloud_monitoring_metrics_asset      --auto-install 2>&1 | tail -2
$CLI add dataframe_flatten_nested_columns    --auto-install 2>&1 | tail -2
$CLI add dataframe_to_bigquery               --auto-install 2>&1 | tail -2

# Populate __init__.py files (CLI doesn't always do this)
echo 'from .component import CloudLoggingQueryAssetComponent
__all__ = ["CloudLoggingQueryAssetComponent"]' > "src/$PKG/components/cloud_logging_query_asset/__init__.py"
echo 'from .component import CloudMonitoringMetricsAssetComponent
__all__ = ["CloudMonitoringMetricsAssetComponent"]' > "src/$PKG/components/cloud_monitoring_metrics_asset/__init__.py"
echo 'from .component import DataframeFlattenNestedColumnsComponent
__all__ = ["DataframeFlattenNestedColumnsComponent"]' > "src/$PKG/components/dataframe_flatten_nested_columns/__init__.py"

# 1) Cloud Logging — recent errors from anywhere in the project
mkdir -p "src/$PKG/defs/recent_errors"
cat > "src/$PKG/defs/recent_errors/defs.yaml" <<EOF
type: $PKG.components.cloud_logging_query_asset.component.CloudLoggingQueryAssetComponent
attributes:
  asset_name: recent_errors
  credentials_path: "$GOOGLE_APPLICATION_CREDENTIALS"
  project_id: "$GCP_PROJECT_ID"
  filter: 'severity>=ERROR'
  lookback_minutes: 1440
  order_by: timestamp desc
  max_entries: 200
  group_name: observability
EOF

# 2) Cloud Monitoring — universal "API request count" metric (always present)
mkdir -p "src/$PKG/defs/api_call_metrics"
cat > "src/$PKG/defs/api_call_metrics/defs.yaml" <<EOF
type: $PKG.components.cloud_monitoring_metrics_asset.component.CloudMonitoringMetricsAssetComponent
attributes:
  asset_name: api_call_metrics
  credentials_path: "$GOOGLE_APPLICATION_CREDENTIALS"
  project_id: "$GCP_PROJECT_ID"
  filter: 'metric.type="serviceruntime.googleapis.com/api/request_count"'
  lookback_minutes: 60
  alignment_period_seconds: 300
  aligner: SUM
  group_name: observability
EOF

# 3+4) Flatten dict/list columns via dataframe_flatten_nested_columns component
mkdir -p "src/$PKG/defs/errors_flat" "src/$PKG/defs/metrics_flat"
cat > "src/$PKG/defs/errors_flat/defs.yaml" <<EOF
type: $PKG.components.dataframe_flatten_nested_columns.component.DataframeFlattenNestedColumnsComponent
attributes:
  asset_name: errors_flat
  upstream_asset_key: recent_errors
  group_name: observability
EOF
cat > "src/$PKG/defs/metrics_flat/defs.yaml" <<EOF
type: $PKG.components.dataframe_flatten_nested_columns.component.DataframeFlattenNestedColumnsComponent
attributes:
  asset_name: metrics_flat
  upstream_asset_key: api_call_metrics
  group_name: observability
EOF

# 5+6) BigQuery sinks
mkdir -p "src/$PKG/defs/errors_bq" "src/$PKG/defs/metrics_bq"
cat > "src/$PKG/defs/errors_bq/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_bigquery.component.DataframeToBigqueryComponent
attributes:
  asset_name: errors_bq
  upstream_asset_key: errors_flat
  table_id: $GCP_PROJECT_ID.$BQ_DATASET.gcp_observability_errors
  credentials_env_var: GOOGLE_APPLICATION_CREDENTIALS
  write_disposition: WRITE_TRUNCATE
  description: Recent ERROR+ entries from Cloud Logging, last 24h.
  group_name: warehouse
EOF
cat > "src/$PKG/defs/metrics_bq/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_bigquery.component.DataframeToBigqueryComponent
attributes:
  asset_name: metrics_bq
  upstream_asset_key: metrics_flat
  table_id: $GCP_PROJECT_ID.$BQ_DATASET.gcp_observability_api_metrics
  credentials_env_var: GOOGLE_APPLICATION_CREDENTIALS
  write_disposition: WRITE_TRUNCATE
  description: API request_count time-series, last 1h, 5-min buckets.
  group_name: warehouse
EOF

cat <<MSG

>>> Setup complete.

Asset graph:
    recent_errors           ← cloud_logging_query_asset (severity>=ERROR, 24h)
          └── errors_flat   ← dataframe_flatten_nested_columns
                   └── errors_bq    ← dataframe_to_bigquery

    api_call_metrics        ← cloud_monitoring_metrics_asset (api/request_count, 1h)
          └── metrics_flat  ← dataframe_flatten_nested_columns
                   └── metrics_bq   ← dataframe_to_bigquery

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Inspect:
    bq query --use_legacy_sql=false \\
      "SELECT severity, COUNT(*) AS n FROM \\\`$GCP_PROJECT_ID.$BQ_DATASET.gcp_observability_errors\\\` GROUP BY severity ORDER BY n DESC"

    bq query --use_legacy_sql=false \\
      "SELECT resource_type, SUM(value) AS calls FROM \\\`$GCP_PROJECT_ID.$BQ_DATASET.gcp_observability_api_metrics\\\` GROUP BY resource_type"
MSG
