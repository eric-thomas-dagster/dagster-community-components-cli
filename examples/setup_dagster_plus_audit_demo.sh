#!/usr/bin/env bash
# Dagster+ Audit Log → SIEM demo.
#
# REQUIRES Dagster+ credentials — this is a "Dagster+ only" demo. The pipeline
# pulls audit-log entries from your Dagster+ deployment via the GraphQL API,
# normalizes them to OCSF, and writes a CSV (so you can validate end-to-end
# without setting up Splunk or Sentinel for the demo). Swap `dataframe_to_csv`
# for the SIEM sink of your choice once the pull works.
#
# What you need before running:
#   - DAGSTER_PLUS_USER_TOKEN: a user token from your Dagster+ deployment
#                              (Settings → Tokens → User Tokens)
#   - Your endpoint URL:
#       US:  https://<org>.dagster.cloud/<deployment>/graphql
#       EU:  https://<org>.eu.dagster.cloud/<deployment>/graphql
#
# Pipeline (3 components):
#   dagster_plus_audit_log_ingestion → siem_event_normalizer (OCSF) → dataframe_to_csv

set -euo pipefail

PROJECT_DIR="${1:-dagster-plus-audit-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding deps"
uv add -q pandas requests
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 3 community components"
$CLI add dagster_plus_audit_log_ingestion --auto-install
$CLI add siem_event_normalizer            --auto-install
$CLI add dataframe_to_csv                 --auto-install

# ── DAGSTER+ ENDPOINT — edit this for your org/deployment, or set DAGSTER_PLUS_ENDPOINT_URL ──
ENDPOINT_URL="${DAGSTER_PLUS_ENDPOINT_URL:-https://YOUR-ORG.dagster.cloud/prod/graphql}"

echo ">>> Writing demo defs.yaml"

cat > "src/$PKG/defs/dagster_plus_audit_log_ingestion/defs.yaml" <<EOF
type: $PKG.components.dagster_plus_audit_log_ingestion.component.DagsterPlusAuditLogIngestionComponent
attributes:
  asset_name: dagster_plus_audit_raw
  endpoint_url: $ENDPOINT_URL
  user_token_env: DAGSTER_PLUS_USER_TOKEN

  # Filter to logins for the demo — tighten/loosen as you like
  event_types:
    - LOG_IN
  # user_emails:
  #   - user@example.com
  # deployment_names:
  #   - prod

  page_size: 100
  lookback_minutes: 1440   # last 24h
  group_name: dagster_plus
  description: "Dagster+ audit-log entries (LOG_IN events, last 24h)"
EOF

cat > "src/$PKG/defs/siem_event_normalizer/defs.yaml" <<EOF
type: $PKG.components.siem_event_normalizer.component.SiemEventNormalizerComponent
attributes:
  asset_name: dagster_plus_audit_ocsf
  upstream_asset_key: dagster_plus_audit_raw
  schema: ocsf
  source_kind: generic
  timestamp_column: timestamp
  actor_column: userEmail
  action_column: eventType
  drop_extras: false
  group_name: normalize
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: dagster_plus_audit_report
  upstream_asset_key: dagster_plus_audit_ocsf
  file_path: /tmp/dagster_plus_audit_ocsf.csv
  include_index: false
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Required env vars before materializing:
    export DAGSTER_PLUS_USER_TOKEN='your-user-token-here'

If you have not already edited the endpoint URL in
    src/$PKG/defs/dagster_plus_audit_log_ingestion/defs.yaml
do that now (or re-run this script with DAGSTER_PLUS_ENDPOINT_URL set).

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Output:
    /tmp/dagster_plus_audit_ocsf.csv  (OCSF-normalized audit log)

Then to ship to a real SIEM, replace the dataframe_to_csv defs.yaml with one
of these sinks (all live in the registry):
    audit_logs_to_splunk
    audit_logs_to_sentinel
    audit_logs_to_datadog_logs
    audit_logs_to_sumo_logic
    audit_logs_to_qradar
    audit_logs_to_chronicle
    audit_logs_to_elastic_security

Or use the all-in-one compound op-job:
    dagster_plus_to_siem_job   # pull → normalize → ship, on cron, single YAML
MSG
