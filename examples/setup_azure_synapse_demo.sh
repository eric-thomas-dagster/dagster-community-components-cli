#!/usr/bin/env bash
# Azure Synapse Analytics demo.
#
# WHAT THIS DEMONSTRATES
#   The azure_synapse component imports an existing Synapse workspace into
#   Dagster: Synapse pipelines become Dagster external assets that can be
#   triggered + polled. Same pattern as azure_data_factory but for Synapse
#   workspaces (a more all-in-one Microsoft analytics platform with
#   integrated Spark + serverless SQL + dedicated SQL pools).
#
# Pipeline:
#   azure_synapse imports the workspace →
#       1 external asset per Synapse pipeline (here: demo_wait_pipeline)
#
# PREREQS
#   1. Azure subscription, az CLI signed in.
#   2. Microsoft.Synapse + Microsoft.Storage providers registered.
#   3. ADLS Gen2 storage account with hierarchical namespace.
#   4. Synapse workspace + at least one pipeline.
#   5. Service principal with "Synapse Contributor" role on the workspace.
#
# REQUIRED ENV VARS
#   AZURE_TENANT_ID        directory tenant ID
#   AZURE_CLIENT_ID        service principal app ID
#   AZURE_CLIENT_SECRET    service principal password
#   AZURE_SUBSCRIPTION_ID  subscription containing the Synapse workspace
#   SYNAPSE_WORKSPACE      workspace name
#
# COST while running
#   Workspace itself is FREE. Serverless SQL pool: $5 per TB scanned ($0
#   for this import-only demo). Apache Spark pools / dedicated SQL pools
#   are $$$ — this demo uses neither.
#
# TEARDOWN
#   az synapse workspace delete -g dagster-demo-rg -n <workspace> --yes
#   az storage account delete  -g dagster-demo-rg -n <storage>   --yes

set -euo pipefail
PROJECT_DIR="${1:-azure-synapse-demo}"

missing=()
[ -z "${AZURE_TENANT_ID:-}" ]       && missing+=("AZURE_TENANT_ID")
[ -z "${AZURE_CLIENT_ID:-}" ]       && missing+=("AZURE_CLIENT_ID")
[ -z "${AZURE_CLIENT_SECRET:-}" ]   && missing+=("AZURE_CLIENT_SECRET")
[ -z "${AZURE_SUBSCRIPTION_ID:-}" ] && missing+=("AZURE_SUBSCRIPTION_ID")
[ -z "${SYNAPSE_WORKSPACE:-}" ]     && missing+=("SYNAPSE_WORKSPACE")

if [ ${#missing[@]} -gt 0 ]; then
  cat <<'NEED'
ERROR: missing env vars: see top of script.

To provision a Synapse workspace + service principal + a tiny demo pipeline:

    RG=dagster-demo-rg
    SYN=dgsyn$(openssl rand -hex 4)
    ST=dgsynst$(openssl rand -hex 3)
    USER=dagsteradmin
    PASS="P$(openssl rand -hex 12)!Aa"
    LOC=westus3   # try eastus first; fall back if SQL capacity is restricted

    az group create -n "$RG" -l "$LOC" 2>/dev/null || true
    az provider register --namespace Microsoft.Synapse --wait
    az provider register --namespace Microsoft.Storage --wait

    # ADLS Gen2 storage (must have HNS) + filesystem
    az storage account create -g "$RG" -n "$ST" -l "$LOC" \
        --sku Standard_LRS --kind StorageV2 --hierarchical-namespace true
    az storage fs create -n synapsefs --account-name "$ST" --auth-mode login

    # Synapse workspace
    az synapse workspace create -g "$RG" -n "$SYN" -l "$LOC" \
        --storage-account "$ST" --file-system synapsefs \
        --sql-admin-login-user "$USER" --sql-admin-login-password "$PASS"

    # Allow your IP through the Synapse firewall
    MYIP=$(curl -s https://api.ipify.org)
    az synapse workspace firewall-rule create -g "$RG" --workspace-name "$SYN" \
        --name AllowMyIP --start-ip-address "$MYIP" --end-ip-address "$MYIP"

    # Service principal for the Dagster component
    SUB=$(az account show --query id -o tsv)
    az ad sp create-for-rbac --name "dagster-synapse-sp" \
        --scopes "/subscriptions/$SUB/resourceGroups/$RG" --years 1 > /tmp/sp.json
    SP_APP=$(jq -r .appId /tmp/sp.json)
    SP_OBJ=$(az ad sp show --id "$SP_APP" --query id -o tsv)
    # "Synapse Administrator" is needed for pipeline execution because
    # creating a pipeline run requires the credential/useSecret action,
    # which "Synapse Contributor" alone does not grant.
    az synapse role assignment create --workspace-name "$SYN" \
        --role "Synapse Administrator" --assignee "$SP_OBJ"

    # Trivial demo pipeline (Wait 5s)
    cat > /tmp/syn_pipe.json <<'PIPE'
    {"properties":{"activities":[{"name":"WaitFiveSeconds","type":"Wait","typeProperties":{"waitTimeInSeconds":5}}]}}
PIPE
    az synapse pipeline create --workspace-name "$SYN" \
        --name demo_wait_pipeline --file @/tmp/syn_pipe.json

    export AZURE_TENANT_ID=$(jq -r .tenant /tmp/sp.json)
    export AZURE_CLIENT_ID=$SP_APP
    export AZURE_CLIENT_SECRET=$(jq -r .password /tmp/sp.json)
    export AZURE_SUBSCRIPTION_ID=$SUB
    export SYNAPSE_WORKSPACE=$SYN
NEED
  exit 1
fi

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q azure-identity azure-mgmt-synapse "azure-synapse-artifacts<1.0"
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 1 community component"
$CLI add azure_synapse --auto-install

echo ">>> Writing demo defs.yaml"

cat > "src/$PKG/defs/azure_synapse/defs.yaml" <<EOF
type: $PKG.components.azure_synapse.component.AzureSynapseComponent
attributes:
  subscription_id: $AZURE_SUBSCRIPTION_ID
  resource_group_name: dagster-demo-rg
  workspace_name: $SYNAPSE_WORKSPACE
  tenant_id: "{{ env('AZURE_TENANT_ID') }}"
  client_id: "{{ env('AZURE_CLIENT_ID') }}"
  client_secret: "{{ env('AZURE_CLIENT_SECRET') }}"
  import_pipelines: true
  import_spark_jobs: false
  import_notebooks: false
  group_name: synapse
  generate_sensor: false
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg list defs       # confirm 1 external asset for demo_wait_pipeline
    uv run dg launch --assets '*'

Verify the Synapse pipeline ran:
    az synapse pipeline-run query-by-workspace --workspace-name "\$SYNAPSE_WORKSPACE" \\
        --filters operand=PipelineName operator=Equals values=demo_wait_pipeline \\
        --last-updated-after "\$(date -u -v-1H +%Y-%m-%dT%H:%M:%SZ)" \\
        --last-updated-before "\$(date -u +%Y-%m-%dT%H:%M:%SZ)"

Teardown:
    az synapse workspace delete -g dagster-demo-rg -n "\$SYNAPSE_WORKSPACE" --yes
    az storage account delete  -g dagster-demo-rg -n <storage> --yes
MSG
