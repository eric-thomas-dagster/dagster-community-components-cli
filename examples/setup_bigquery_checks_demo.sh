#!/usr/bin/env bash
# BigQuery asset checks demo — cost guardrail + freshness SLO.
#
# WHAT THIS DEMONSTRATES (validated live against servicepulse-490502)
#   Both BQ asset checks on a BQ table:
#     bigquery_dry_run_check        cost ceiling (bytes / USD / slot-ms)
#     bigquery_table_freshness_check freshness SLO via last_modified_time
#
# Asset graph:
#   warehouse_table  (declare-only external BQ asset)
#         │
#         ├── [check] query_cost_guard          ← bigquery_dry_run_check
#         └── [check] freshness_slo             ← bigquery_table_freshness_check
#
# REQUIRED ENV VARS
#   GOOGLE_APPLICATION_CREDENTIALS  service-account JSON path
#   GCP_PROJECT_ID                  project id
#   BQ_TABLE                        existing fully-qualified table id
#                                   e.g. servicepulse-490502.dagster_demo.gcp_observability_errors
#
# REQUIRED APIS
#   BigQuery  https://console.cloud.google.com/apis/library/bigquery.googleapis.com
#
# REQUIRED IAM
#   roles/bigquery.dataViewer + roles/bigquery.jobUser on the project
#
# COST while running
#   Free. Dry-run + get_table are metadata operations — no data scanned.

set -euo pipefail
PROJECT_DIR="${1:-bigquery-checks-demo}"

if [ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]; then
  echo "ERROR: set GOOGLE_APPLICATION_CREDENTIALS"; exit 1
fi
if [ -z "${GCP_PROJECT_ID:-}" ]; then
  echo "ERROR: set GCP_PROJECT_ID"; exit 1
fi
BQ_TABLE="${BQ_TABLE:-$GCP_PROJECT_ID.dagster_demo.gcp_observability_errors}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas google-auth google-cloud-bigquery
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing components"
$CLI add external_bigquery_table         --auto-install 2>&1 | tail -2
$CLI add bigquery_dry_run_check          --auto-install 2>&1 | tail -2
$CLI add bigquery_table_freshness_check  --auto-install 2>&1 | tail -2

echo 'from .component import ExternalBigqueryTableComponent
__all__ = ["ExternalBigqueryTableComponent"]' > "src/$PKG/components/external_bigquery_table/__init__.py"
echo 'from .component import BigqueryDryRunCheckComponent
__all__ = ["BigqueryDryRunCheckComponent"]' > "src/$PKG/components/bigquery_dry_run_check/__init__.py"
echo 'from .component import BigqueryTableFreshnessCheckComponent
__all__ = ["BigqueryTableFreshnessCheckComponent"]' > "src/$PKG/components/bigquery_table_freshness_check/__init__.py"

# Remove auto-installed example defs (their asset names collide with ours)
rm -rf "src/$PKG/defs/external_bigquery_table" "src/$PKG/defs/bigquery_dry_run_check" "src/$PKG/defs/bigquery_table_freshness_check"

# 1) Declare-only external asset (the BQ table)
mkdir -p "src/$PKG/defs/warehouse_table"
# Parse $BQ_TABLE = project.dataset.table into pieces
BQ_PROJECT="${BQ_TABLE%%.*}"
BQ_DT_AND_TBL="${BQ_TABLE#*.}"
BQ_DATASET="${BQ_DT_AND_TBL%.*}"
BQ_TBL="${BQ_TABLE##*.}"
cat > "src/$PKG/defs/warehouse_table/defs.yaml" <<EOF
type: $PKG.components.external_bigquery_table.component.ExternalBigqueryTableComponent
attributes:
  asset_key: warehouse_table
  project_id: $BQ_PROJECT
  dataset_id: $BQ_DATASET
  table_id: $BQ_TBL
  description: "Existing BQ table $BQ_TABLE — declare-only; refreshed externally."
  group_name: warehouse
EOF

# 2) Cost-guard check
mkdir -p "src/$PKG/defs/cost_guard"
cat > "src/$PKG/defs/cost_guard/defs.yaml" <<EOF
type: $PKG.components.bigquery_dry_run_check.component.BigqueryDryRunCheckComponent
attributes:
  asset_key: warehouse_table
  check_name: query_cost_guard
  credentials_path: "$GOOGLE_APPLICATION_CREDENTIALS"
  project_id: "$GCP_PROJECT_ID"
  sql: |
    SELECT severity, COUNT(*) AS n
    FROM \`$BQ_TABLE\`
    GROUP BY severity
  max_bytes: 10000000          # 10 MB — tiny query, should pass
  max_cost_usd: 0.001          # convenience cap; computed locally
  on_demand_price_per_tb_usd: 6.25
  severity: ERROR
  blocking: false
EOF

# 3) Freshness check
mkdir -p "src/$PKG/defs/freshness_slo"
cat > "src/$PKG/defs/freshness_slo/defs.yaml" <<EOF
type: $PKG.components.bigquery_table_freshness_check.component.BigqueryTableFreshnessCheckComponent
attributes:
  asset_key: warehouse_table
  check_name: freshness_slo
  credentials_path: "$GOOGLE_APPLICATION_CREDENTIALS"
  table_id: "$BQ_TABLE"
  max_age_minutes: 1440        # 24h SLO
  severity: ERROR
  blocking: false
EOF

cat <<MSG

>>> Setup complete.

Asset:    warehouse_table (external, declare-only)
Checks:
  ✓ query_cost_guard   max 10 MB / \$0.001 / on-demand
  ✓ freshness_slo      max 24h since last_modified_time

Materialize all checks:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Both checks should PASS for a recently-written table under 10 MB.

To see the cost-guard FAIL, lower max_bytes to 100 (100 bytes) in cost_guard/defs.yaml.
To see the freshness FAIL, lower max_age_minutes to 1.
MSG
