#!/usr/bin/env bash
# Azure Synapse Serverless SQL — query parquet files in ADLS without compute.
#
# WHAT THIS DEMONSTRATES
#   No new Synapse-specific component required! The existing generic
#   `dataframe_to_adls` + `dataframe_from_sql` components together cover
#   the killer Synapse Serverless workflow:
#
#   100 synthetic orders → DataFrame → parquet on ADLS Gen2 →
#       Synapse Serverless OPENROWSET aggregation → DataFrame asset
#
#   Synapse Serverless SQL is FREE for the first 1TB scanned per month
#   and provisions zero compute — just point at parquet/csv in ADLS and
#   write T-SQL.
#
# Pipeline (3 components, all already in the registry):
#   synthetic_data_generator → dataframe_to_adls (parquet) →
#       dataframe_from_sql (OPENROWSET via Synapse Serverless)
#
# PREREQS
#   1. Azure subscription, az CLI signed in.
#   2. Microsoft.Synapse + Microsoft.Storage providers registered.
#   3. ADLS Gen2 storage account (HNS enabled), Synapse workspace, demo db
#      with master key + DATABASE SCOPED CREDENTIAL + EXTERNAL DATA SOURCE.
#      See "Provisioning" below.
#
# REQUIRED ENV VARS
#   AZURE_STORAGE_ACCOUNT       storage account name
#   AZURE_STORAGE_ACCOUNT_KEY   storage account key
#   SYNAPSE_DEMO_URL            mssql+pymssql://<user>:<pwd>@<workspace>-ondemand.sql.azuresynapse.net:1433/demo
#
# COST while running
#   Workspace itself: $0. Serverless SQL queries: $0 if total scanned
#   < 1TB/month (this demo's 100 rows scans <1MB). Storage: $0.018/GB/mo.
#
# TEARDOWN
#   az synapse workspace delete -g dagster-demo-rg -n <workspace> --yes
#   az storage account delete  -g dagster-demo-rg -n <storage>   --yes

set -euo pipefail
PROJECT_DIR="${1:-azure-synapse-serverless-demo}"

missing=()
[ -z "${AZURE_STORAGE_ACCOUNT:-}" ]     && missing+=("AZURE_STORAGE_ACCOUNT")
[ -z "${AZURE_STORAGE_ACCOUNT_KEY:-}" ] && missing+=("AZURE_STORAGE_ACCOUNT_KEY")
[ -z "${SYNAPSE_DEMO_URL:-}" ]          && missing+=("SYNAPSE_DEMO_URL")

if [ ${#missing[@]} -gt 0 ]; then
  cat <<'NEED'
ERROR: missing env vars: see top of script.

To provision a Synapse workspace + ADLS storage + the OPENROWSET prereqs:

    RG=dagster-demo-rg
    SYN=dgsyn$(openssl rand -hex 4)
    ST=dgsynst$(openssl rand -hex 3)
    USER=dagsteradmin
    PASS="P$(openssl rand -hex 12)!Aa"

    az group create -n "$RG" -l westus3 2>/dev/null || true
    az provider register --namespace Microsoft.Synapse --wait
    az provider register --namespace Microsoft.Storage --wait

    az storage account create -g "$RG" -n "$ST" -l westus3 \
        --sku Standard_LRS --kind StorageV2 --hierarchical-namespace true
    az storage fs create -n synapsefs --account-name "$ST" --auth-mode login

    az synapse workspace create -g "$RG" -n "$SYN" -l westus3 \
        --storage-account "$ST" --file-system synapsefs \
        --sql-admin-login-user "$USER" --sql-admin-login-password "$PASS"

    MYIP=$(curl -s https://api.ipify.org)
    az synapse workspace firewall-rule create -g "$RG" --workspace-name "$SYN" \
        --name AllowMyIP --start-ip-address "$MYIP" --end-ip-address "$MYIP"

    # Grant the workspace MSI Storage Blob Data Reader on the storage account
    WS_MSI=$(az synapse workspace show -g "$RG" -n "$SYN" --query identity.principalId -o tsv)
    SUB=$(az account show --query id -o tsv)
    az role assignment create --assignee "$WS_MSI" \
        --role "Storage Blob Data Reader" \
        --scope "/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.Storage/storageAccounts/$ST"

    # Build URL pointing at master, then create demo db, master key, credential, EDS
    PASS_ENC=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$PASS")
    MASTER_URL="mssql+pymssql://$USER:$PASS_ENC@$SYN-ondemand.sql.azuresynapse.net:1433/master"
    python3 -c "
    import sqlalchemy as sa, os
    eng = sa.create_engine('$MASTER_URL', isolation_level='AUTOCOMMIT')
    with eng.connect() as c:
        c.execute(sa.text('CREATE DATABASE demo'))
    "

    DEMO_URL="mssql+pymssql://$USER:$PASS_ENC@$SYN-ondemand.sql.azuresynapse.net:1433/demo"
    python3 -c "
    import sqlalchemy as sa, os
    eng = sa.create_engine('$DEMO_URL', isolation_level='AUTOCOMMIT')
    with eng.connect() as c:
        c.execute(sa.text(\"\"\"CREATE MASTER KEY ENCRYPTION BY PASSWORD = '$PASS'\"\"\"))
        c.execute(sa.text(\"\"\"CREATE DATABASE SCOPED CREDENTIAL [synapsemsi] WITH IDENTITY = 'Managed Identity'\"\"\"))
        c.execute(sa.text(\"\"\"CREATE EXTERNAL DATA SOURCE [synapsefs_ds] WITH (LOCATION = 'https://$ST.dfs.core.windows.net/synapsefs', CREDENTIAL = [synapsemsi])\"\"\"))
    "

    export AZURE_STORAGE_ACCOUNT=$ST
    export AZURE_STORAGE_ACCOUNT_KEY=$(az storage account keys list -g "$RG" --account-name "$ST" --query "[0].value" -o tsv)
    export SYNAPSE_DEMO_URL=$DEMO_URL
NEED
  exit 1
fi

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas sqlalchemy pymssql adlfs pyarrow
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 3 community components"
$CLI add synthetic_data_generator --auto-install
$CLI add dataframe_to_adls        --auto-install
$CLI add dataframe_from_sql       --auto-install

echo ">>> Writing demo defs.yaml"

cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: orders_raw
  schema_type: orders
  row_count: 100
  random_state: 42
  group_name: ingest
EOF

cat > "src/$PKG/defs/dataframe_to_adls/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_adls.component.DataframeToAdlsComponent
attributes:
  asset_name: orders_in_synapse_storage
  upstream_asset_key: orders_raw
  account_name_env_var: AZURE_STORAGE_ACCOUNT
  account_key_env_var: AZURE_STORAGE_ACCOUNT_KEY
  container: synapsefs
  blob_path: dagster-test/orders.parquet
  format: parquet
  group_name: storage
EOF

cat > "src/$PKG/defs/dataframe_from_sql/defs.yaml" <<EOF
type: $PKG.components.dataframe_from_sql.component.DataframeFromSqlComponent
attributes:
  asset_name: orders_revenue_summary
  database_url_env_var: SYNAPSE_DEMO_URL
  query: |
    SELECT category, COUNT(*) AS n, SUM(total) AS revenue
    FROM OPENROWSET(
        BULK 'dagster-test/orders.parquet',
        DATA_SOURCE = 'synapsefs_ds',
        FORMAT = 'PARQUET'
    ) AS o
    GROUP BY category
  deps: [orders_in_synapse_storage]
  group_name: analytics
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

The demo will:
  1. Generate 100 synthetic orders
  2. Write them as parquet to synapsefs/dagster-test/orders.parquet
  3. Run OPENROWSET in Synapse Serverless to aggregate revenue by category
     -> 7 rows × 3 columns ('Books', 'Clothing', 'Electronics', 'Food', 'Sports', 'Toys', + 1 misc)

Teardown:
    az synapse workspace delete -g dagster-demo-rg -n <workspace> --yes
    az storage account delete  -g dagster-demo-rg -n <storage>   --yes
MSG
