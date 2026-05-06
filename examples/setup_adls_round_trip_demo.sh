#!/usr/bin/env bash
# Azure ADLS Gen2 round-trip demo.
#
# WHAT THIS DEMONSTRATES
#   Synthetic e-commerce orders → write Parquet to ADLS Gen2 → declare the
#   landed Parquet as an observable external asset (lineage on the cold side
#   of the lake).
#
# PREREQS — read carefully before running
#   1. An Azure Pay-As-You-Go (or higher) subscription you own.
#   2. Azure CLI installed + signed in (`az login`).
#   3. The Microsoft.Storage resource provider registered for that subscription
#      (one-time:  `az provider register --namespace Microsoft.Storage --wait`).
#   4. An ADLS Gen2 storage account + container — see "Provisioning" below
#      if you need to create them. Plan on ~$0.05/month for a small Standard_LRS
#      account; teardown is a single `az group delete`.
#
# REQUIRED ENV VARS at run time
#   AZURE_STORAGE_ACCOUNT_NAME   the storage account name (lowercase, alphanum)
#   AZURE_STORAGE_ACCOUNT_KEY    one of the two access keys
#                                 (`az storage account keys list -g <rg> -n <sa>`)
#
# COST while running
#   Storage:  small Standard_LRS account, < $0.05/month for these tiny files.
#   Compute:  none (Dagster runs locally).
#
# TEARDOWN when done
#   az group delete --name dagster-demo-rg --yes
#
# Pipeline (3 components, all autoloaded by `dg`):
#   synthetic_data_generator → dataframe_to_adls → external_adls_asset

set -euo pipefail
PROJECT_DIR="${1:-adls-round-trip-demo}"

if [ -z "${AZURE_STORAGE_ACCOUNT_NAME:-}" ] || [ -z "${AZURE_STORAGE_ACCOUNT_KEY:-}" ]; then
  cat <<'NEED_CREDS'
ERROR: this demo needs Azure storage credentials. Set:
    export AZURE_STORAGE_ACCOUNT_NAME=<your-account-name>
    export AZURE_STORAGE_ACCOUNT_KEY=<your-account-key>

To provision a fresh storage account:
    az group create --name dagster-demo-rg --location eastus
    az storage account create -g dagster-demo-rg -n dagsterdemo$RANDOM \
        -l eastus --sku Standard_LRS --kind StorageV2 --hns true
    az storage container create --name demo \
        --account-name <name> \
        --account-key $(az storage account keys list -g dagster-demo-rg -n <name> --query '[0].value' -o tsv)
NEED_CREDS
  exit 1
fi

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas pyarrow
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 3 community components"
$CLI add synthetic_data_generator --auto-install
$CLI add dataframe_to_adls        --auto-install
$CLI add external_adls_asset      --auto-install

echo ">>> Writing demo defs.yaml"

cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: orders_raw
  schema_type: orders
  row_count: 200
  random_state: 42
  description: 200 synthetic e-commerce orders
  group_name: ingest
EOF

cat > "src/$PKG/defs/dataframe_to_adls/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_adls.component.DataframeToAdlsComponent
attributes:
  asset_name: orders_in_adls
  upstream_asset_key: orders_raw
  account_name_env_var: AZURE_STORAGE_ACCOUNT_NAME
  account_key_env_var: AZURE_STORAGE_ACCOUNT_KEY
  container: demo
  blob_path: round_trip/orders.parquet
  format: parquet
  compression: snappy
  group_name: adls_sink
EOF

cat > "src/$PKG/defs/external_adls_asset/defs.yaml" <<EOF
type: $PKG.components.external_adls_asset.component.ExternalAdlsAsset
attributes:
  asset_key: adls_orders_landed
  account_name: $AZURE_STORAGE_ACCOUNT_NAME
  container_name: demo
  path_prefix: round_trip/
  group_name: adls_observed
  description: Observable external asset — declares the landed Parquet exists in ADLS for lineage
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Verify the Parquet landed in ADLS:
    az storage blob list \\
        --account-name "\$AZURE_STORAGE_ACCOUNT_NAME" \\
        --account-key "\$AZURE_STORAGE_ACCOUNT_KEY" \\
        --container-name demo --prefix round_trip/ \\
        --query '[].{name:name, size:properties.contentLength}' -o table

Inspect the file:
    az storage blob download \\
        --account-name "\$AZURE_STORAGE_ACCOUNT_NAME" \\
        --account-key "\$AZURE_STORAGE_ACCOUNT_KEY" \\
        --container-name demo --name round_trip/orders.parquet \\
        --file /tmp/orders_landed.parquet
    uv run python -c "import pandas; print(pandas.read_parquet('/tmp/orders_landed.parquet').head())"

Teardown when done (deletes everything in the resource group):
    az group delete --name dagster-demo-rg --yes
MSG
