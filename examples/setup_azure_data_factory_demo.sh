#!/usr/bin/env bash
# Azure Data Factory demo.
#
# WHAT THIS DEMONSTRATES
#   The azure_data_factory component imports an existing ADF instance into
#   Dagster: ADF pipelines become Dagster external assets, ADF triggers
#   become Dagster schedules. The pattern is "Dagster as the orchestration
#   pane on top of ADF" — useful for teams migrating off ADF gradually
#   while keeping existing pipelines running.
#
# Pipeline:
#   azure_data_factory imports the ADF instance →
#       1 external asset per ADF pipeline (here: demo_wait_pipeline)
#
# PREREQS
#   1. Azure subscription, az CLI signed in.
#   2. Microsoft.DataFactory provider registered:
#        az provider register --namespace Microsoft.DataFactory --wait
#   3. An ADF instance with at least one pipeline — see "Provisioning".
#   4. A service principal with "Data Factory Contributor" on the RG.
#
# REQUIRED ENV VARS
#   AZURE_TENANT_ID        directory tenant ID (from `az ad sp create-for-rbac`)
#   AZURE_CLIENT_ID        service principal app ID
#   AZURE_CLIENT_SECRET    service principal password
#   AZURE_SUBSCRIPTION_ID  subscription containing the ADF
#
# COST while running
#   ADF instance is FREE. Charges are per-activity-run (~$0.001 per Wait
#   activity here). For this demo's import-only flow, you pay nothing.
#
# TEARDOWN
#   az datafactory delete -g dagster-demo-rg --factory-name <name> --yes
#   az ad sp delete --id <sp-app-id>

set -euo pipefail
PROJECT_DIR="${1:-azure-data-factory-demo}"

missing=()
[ -z "${AZURE_TENANT_ID:-}" ]       && missing+=("AZURE_TENANT_ID")
[ -z "${AZURE_CLIENT_ID:-}" ]       && missing+=("AZURE_CLIENT_ID")
[ -z "${AZURE_CLIENT_SECRET:-}" ]   && missing+=("AZURE_CLIENT_SECRET")
[ -z "${AZURE_SUBSCRIPTION_ID:-}" ] && missing+=("AZURE_SUBSCRIPTION_ID")
[ -z "${ADF_FACTORY_NAME:-}" ]      && missing+=("ADF_FACTORY_NAME")

if [ ${#missing[@]} -gt 0 ]; then
  cat <<'NEED'
ERROR: missing env vars: see top of script.

To provision an ADF instance + service principal + a tiny demo pipeline:

    RG=dagster-demo-rg
    ADF=dagsterdemoadf$(openssl rand -hex 3)

    az group create -n "$RG" -l eastus
    az provider register --namespace Microsoft.DataFactory --wait
    az datafactory create -g "$RG" --factory-name "$ADF" -l eastus

    # Service principal scoped to the RG only
    SUB=$(az account show --query id -o tsv)
    az ad sp create-for-rbac --name "dagster-adf-sp" \
        --role "Data Factory Contributor" \
        --scopes "/subscriptions/$SUB/resourceGroups/$RG" \
        --years 1 > /tmp/sp.json 2>&1
    APP=$(jq -r .appId /tmp/sp.json)
    PWD=$(jq -r .password /tmp/sp.json)
    TEN=$(jq -r .tenant /tmp/sp.json)

    # Create a trivial pipeline (Wait 5s)
    cat > /tmp/pipe.json <<'EOF2'
    {"name":"demo_wait_pipeline","properties":{"activities":[
        {"name":"WaitFiveSeconds","type":"Wait","typeProperties":{"waitTimeInSeconds":5}}]}}
EOF2
    az datafactory pipeline create -g "$RG" --factory-name "$ADF" \
        --name demo_wait_pipeline --pipeline @/tmp/pipe.json

    export AZURE_TENANT_ID=$TEN
    export AZURE_CLIENT_ID=$APP
    export AZURE_CLIENT_SECRET=$PWD
    export AZURE_SUBSCRIPTION_ID=$SUB
    export ADF_FACTORY_NAME=$ADF
NEED
  exit 1
fi

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q azure-identity azure-mgmt-datafactory
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 1 community component"
$CLI add azure_data_factory --auto-install

echo ">>> Writing demo defs.yaml"

cat > "src/$PKG/defs/azure_data_factory/defs.yaml" <<EOF
type: $PKG.components.azure_data_factory.component.AzureDataFactoryComponent
attributes:
  subscription_id: $AZURE_SUBSCRIPTION_ID
  resource_group_name: dagster-demo-rg
  factory_name: $ADF_FACTORY_NAME

  # Service principal auth — the component reads these env vars at load time
  tenant_id_env_var: AZURE_TENANT_ID
  client_id_env_var: AZURE_CLIENT_ID
  client_secret_env_var: AZURE_CLIENT_SECRET

  import_pipelines: true
  group_name: adf

  # Comprehensive features (all optional)
  capture_activity_metadata: true       # surface per-activity status / duration / error / output keys
  max_wait_seconds: 600                  # this Wait pipeline finishes in ~5s; cap to keep tests fast
  run_poll_interval_seconds: 5

  # If you wanted to wire ADF pipelines to Dagster upstreams:
  # upstream_asset_keys: ["dbt_marts/orders_clean"]            # all ADF pipelines wait for this
  # assets_by_pipeline_name:                                    # per-pipeline overrides
  #   demo_wait_pipeline:
  #     deps: ["raw/orders"]
  #     description: "Pipeline gated on the raw orders landing"

  # If you wanted partitioned ADF pipeline runs (one ADF run per partition_key):
  # partition_type: daily
  # partition_start: "2026-04-01"
  # partition_parameter_name: ODATE                            # name ADF expects in pipeline.parameters

  # If you wanted to pass ADF pipeline parameters statically:
  # pipeline_parameters:
  #   environment: production
  #   sla_minutes: 60
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg list defs       # confirm 1 external asset for demo_wait_pipeline
    uv run dg launch --assets '*'

Verify the ADF activity ran (this triggers the pipeline you imported):
    az datafactory pipeline-run query-by-factory \\
        -g dagster-demo-rg --factory-name "\$ADF_FACTORY_NAME" \\
        --last-updated-after "\$(date -u -v-1H +%Y-%m-%dT%H:%M:%SZ)" \\
        --last-updated-before "\$(date -u +%Y-%m-%dT%H:%M:%SZ)"

Teardown:
    az datafactory delete -g dagster-demo-rg --factory-name "\$ADF_FACTORY_NAME" --yes
    # The service principal lives at the AAD level; delete with:
    az ad sp delete --id "\$AZURE_CLIENT_ID"
MSG
