#!/usr/bin/env bash
# Dagster+ → Security Lake asset pipeline demo (Dagster+ required).
#
# Asset-style version of the Dagster+ → SIEM op-job. Lineage tracked because
# this is a real data pipeline: pull events → normalize → validate → write
# OCSF Parquet for AWS Security Lake.
#
# Pipeline (4 components, all assets):
#   dagster_plus_audit_log_ingestion → ocsf_normalizer
#                                          ├─→ ocsf_validator (asset_check)
#                                          └─→ dataframe_to_security_lake (or local parquet)
#
# Required env vars:
#   DAGSTER_PLUS_USER_TOKEN  - user token from Dagster+ Settings → Tokens
#   DAGSTER_PLUS_ENDPOINT_URL  (or edit defs.yaml after scaffolding)
#
# For the Security Lake sink, also:
#   AWS_ACCOUNT_ID (or set in the YAML)
#   plus AWS credentials via standard providers (env / profile / role)
#
# To skip Security Lake (no AWS), pass --local as $2 to write to local parquet.

set -euo pipefail
PROJECT_DIR="${1:-dagster-plus-security-lake-demo}"
SINK_MODE="${2:-local}"   # local | security_lake

echo ">>> Scaffolding"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"
mkdir -p out
PKG="$(ls src/ | head -1)"

uv add -q pandas requests pyarrow tabulate
if [ "$SINK_MODE" = "security_lake" ]; then
  uv add -q boto3
fi
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing components"
$CLI add dagster_plus_audit_log_ingestion --auto-install
$CLI add ocsf_normalizer                  --auto-install
$CLI add ocsf_validator                   --auto-install
if [ "$SINK_MODE" = "security_lake" ]; then
  $CLI add dataframe_to_security_lake     --auto-install
else
  $CLI add dataframe_to_parquet           --auto-install
fi

ENDPOINT_URL="${DAGSTER_PLUS_ENDPOINT_URL:-https://YOUR-ORG.dagster.cloud/prod/graphql}"

echo ">>> Writing demo defs.yaml"

cat > "src/$PKG/defs/dagster_plus_audit_log_ingestion/defs.yaml" <<EOF
type: $PKG.components.dagster_plus_audit_log_ingestion.component.DagsterPlusAuditLogIngestionComponent
attributes:
  asset_name: dagster_plus_audit_raw
  endpoint_url: $ENDPOINT_URL
  user_token_env: DAGSTER_PLUS_USER_TOKEN
  event_types:
    - LOG_IN
  page_size: 100
  lookback_minutes: 1440
  group_name: dagster_plus
EOF

cat > "src/$PKG/defs/ocsf_normalizer/defs.yaml" <<EOF
type: $PKG.components.ocsf_normalizer.component.OcsfNormalizerComponent
attributes:
  asset_name: dagster_plus_audit_ocsf
  upstream_asset_key: dagster_plus_audit_raw
  source_kind: dagster_plus
  vendor_name: Dagster
  product_name: Dagster+
  ocsf_version: "1.1.0"
  default_severity_id: 1
  drop_unmapped: false
  keep_raw: true
  group_name: ocsf
EOF

cat > "src/$PKG/defs/ocsf_validator/defs.yaml" <<EOF
type: $PKG.components.ocsf_validator.component.OcsfValidatorComponent
attributes:
  check_name: ocsf_conformance
  upstream_asset_key: dagster_plus_audit_ocsf
  blocking: false
  require_known_class_uid: true
  max_invalid_rows: 0
EOF

if [ "$SINK_MODE" = "security_lake" ]; then
cat > "src/$PKG/defs/dataframe_to_security_lake/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_security_lake.component.DataframeToSecurityLakeComponent
attributes:
  asset_name: dagster_plus_security_lake
  upstream_asset_key: dagster_plus_audit_ocsf
  bucket: aws-security-data-lake-us-east-1-CHANGEME
  source_location: ext-dagster-plus-audit
  region_name: us-east-1
  account_id: "${AWS_ACCOUNT_ID:-CHANGEME}"
  event_day_field: time
  group_name: security_lake
EOF
else
cat > "src/$PKG/defs/dataframe_to_parquet/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_parquet.component.DataframeToParquetComponent
attributes:
  asset_name: dagster_plus_security_lake_local
  upstream_asset_key: dagster_plus_audit_ocsf
  file_path: out/dagster_plus_audit_ocsf.parquet
  compression: snappy
  group_name: security_lake_local
EOF
fi

cat <<MSG

>>> Setup complete (sink_mode=$SINK_MODE).

Required env vars:
    export DAGSTER_PLUS_USER_TOKEN='your-user-token'
    export DAGSTER_PLUS_ENDPOINT_URL='https://my-org.dagster.cloud/prod/graphql'

If you have not edited the endpoint URL in defs.yaml, do that now (or set
DAGSTER_PLUS_ENDPOINT_URL and re-run this script).

Materialize:
    cd $PROJECT_DIR && uv run dg launch --assets '*'

Output (sink_mode=local):
    $PROJECT_ABS/out/dagster_plus_audit_ocsf.parquet — OCSF rows

Output (sink_mode=security_lake):
    s3://<bucket>/ext-dagster-plus-audit/region=<r>/accountId=<a>/eventDay=<YYYYMMDD>/*.parquet
    Edit src/$PKG/defs/dataframe_to_security_lake/defs.yaml first (set bucket
    + account_id), then materialize.
MSG
