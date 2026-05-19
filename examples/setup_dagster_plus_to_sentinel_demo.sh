#!/usr/bin/env bash
# Dagster+ → Microsoft Sentinel demo.
#
# WHAT THIS DEMONSTRATES
#   Pull Dagster+ audit-log entries via GraphQL → normalize to OCSF v1.1 →
#   ship to Microsoft Sentinel via the Log Analytics ingestion API.
#   The events land in a Custom Logs table (default: DagsterPlusAudit_CL)
#   queryable from Sentinel's KQL workbench.
#
# PREREQS — read carefully before running
#   1. A Dagster+ deployment + a user token from Settings → Tokens → User Tokens.
#   2. Azure subscription you own.
#   3. Azure CLI signed in (`az login`).
#   4. Microsoft.OperationalInsights provider registered (one-time):
#        `az provider register --namespace Microsoft.OperationalInsights --wait`
#   5. A Log Analytics workspace — see "Provisioning" below.
#   6. (Optional) Sentinel solution deployed onto that workspace if you want
#      Sentinel's incident/hunt UX. Custom log ingestion works either way.
#
# REQUIRED ENV VARS at run time
#   DAGSTER_PLUS_USER_TOKEN       Dagster+ user token
#   DAGSTER_PLUS_ENDPOINT_URL     https://<org>.dagster.cloud/<deployment>/graphql
#                                 (or <org>.eu.dagster.cloud for EU)
#   SENTINEL_WORKSPACE_ID         the workspace's customerId (GUID)
#   SENTINEL_WORKSPACE_KEY        primary or secondary shared key
#
# COST while running
#   Workspace: PerGB2018 SKU. First 5GB/month free. This demo sends < 100KB.
#   Effectively $0.
#
# TEARDOWN
#   az group delete --name dagster-demo-rg --yes      # nukes workspace + everything
#   (or just delete the workspace: az monitor log-analytics workspace delete ...)
#
# Pipeline (3 components, all autoloaded by `dg`):
#   dagster_plus_audit_log_ingestion → ocsf_normalizer → audit_logs_to_sentinel

set -euo pipefail
PROJECT_DIR="${1:-dagster-plus-sentinel-demo}"

missing=()
[ -z "${DAGSTER_PLUS_USER_TOKEN:-}" ]    && missing+=("DAGSTER_PLUS_USER_TOKEN")
[ -z "${DAGSTER_PLUS_ENDPOINT_URL:-}" ]  && missing+=("DAGSTER_PLUS_ENDPOINT_URL")
[ -z "${SENTINEL_WORKSPACE_ID:-}" ]      && missing+=("SENTINEL_WORKSPACE_ID")
[ -z "${SENTINEL_WORKSPACE_KEY:-}" ]     && missing+=("SENTINEL_WORKSPACE_KEY")

if [ ${#missing[@]} -gt 0 ]; then
  echo "ERROR: missing required env vars: ${missing[*]}"
  echo
  echo "To provision a fresh Log Analytics workspace + get the IDs:"
  echo "    az group create --name dagster-demo-rg --location eastus"
  echo "    az monitor log-analytics workspace create \\"
  echo "        -g dagster-demo-rg -n dagster-demo-law -l eastus --sku PerGB2018"
  echo "    export SENTINEL_WORKSPACE_ID=\$(az monitor log-analytics workspace show \\"
  echo "        -g dagster-demo-rg -n dagster-demo-law --query customerId -o tsv)"
  echo "    export SENTINEL_WORKSPACE_KEY=\$(az monitor log-analytics workspace get-shared-keys \\"
  echo "        -g dagster-demo-rg -n dagster-demo-law --query primarySharedKey -o tsv)"
  echo
  echo "And for Dagster+:"
  echo "    export DAGSTER_PLUS_USER_TOKEN=<your-token>"
  echo "    export DAGSTER_PLUS_ENDPOINT_URL=https://<org>.dagster.cloud/<deployment>/graphql"
  exit 1
fi

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas requests
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 3 community components"
$CLI add dagster_plus_audit_log_ingestion --auto-install
$CLI add ocsf_normalizer                  --auto-install
$CLI add audit_logs_to_sentinel           --auto-install

echo ">>> Writing demo defs.yaml"

cat > "src/$PKG/defs/dagster_plus_audit_log_ingestion/defs.yaml" <<EOF
type: $PKG.components.dagster_plus_audit_log_ingestion.component.DagsterPlusAuditLogIngestionComponent
attributes:
  asset_name: dagster_plus_audit_raw
  endpoint_url: $DAGSTER_PLUS_ENDPOINT_URL
  user_token_env: DAGSTER_PLUS_USER_TOKEN
  page_size: 100
  lookback_minutes: 525600     # last year — adjust to your needs
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
  group_name: ocsf
EOF

cat > "src/$PKG/defs/audit_logs_to_sentinel/defs.yaml" <<EOF
type: $PKG.components.audit_logs_to_sentinel.component.AuditLogsToSentinelComponent
attributes:
  asset_name: dagster_plus_audit_in_sentinel
  upstream_asset_key: dagster_plus_audit_ocsf
  workspace_id: $SENTINEL_WORKSPACE_ID
  workspace_key_env: SENTINEL_WORKSPACE_KEY
  log_type: DagsterPlusAudit
  batch_size: 200
  group_name: sentinel_sink
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Verify in Sentinel (after ~5min for ingestion latency):
    az monitor log-analytics query \\
        -w "$SENTINEL_WORKSPACE_ID" \\
        --analytics-query 'DagsterPlusAudit_CL | take 10' -o table

Or open the Azure Portal → Log Analytics workspace → Logs and run:
    DagsterPlusAudit_CL
    | summarize count() by class_uid_d
    | render piechart

Teardown:
    az group delete --name dagster-demo-rg --yes
MSG
