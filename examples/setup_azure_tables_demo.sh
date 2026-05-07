#!/usr/bin/env bash
# Azure Tables round-trip demo.
#
# WHAT THIS DEMONSTRATES
#   100 synthetic orders → DataFrame → Azure Table Storage (NoSQL key-value)
#   → read back with OData filter → CSV report.
#
#   Azure Tables is a cheap NoSQL store inside any Azure Storage account
#   ($0.045/GB/mo + $0.00036 per 10K transactions). Way cheaper than Cosmos
#   DB for simple structured data; less powerful (no global distribution,
#   no auto-indexing).
#
# Pipeline (4 components):
#   synthetic_data_generator → dataframe_to_azure_table → azure_table_reader → dataframe_to_csv
#
# PREREQS
#   1. Azure subscription, az CLI signed in.
#   2. Storage account (any kind — Blob, ADLS Gen2, etc. all expose Tables).
#
# REQUIRED ENV VARS
#   AZURE_STORAGE_ACCOUNT       storage account name
#   AZURE_STORAGE_ACCOUNT_KEY   storage account key
#
# COST while running
#   $0.0001 per 10K transactions + $0.045/GB/mo. This demo uses fewer than
#   1K transactions and < 1MB of data — fractions of a cent.
#
# TEARDOWN
#   az storage table delete --account-name <storage> --name dagsterorders --auth-mode key

set -euo pipefail
PROJECT_DIR="${1:-azure-tables-demo}"

missing=()
[ -z "${AZURE_STORAGE_ACCOUNT:-}" ]     && missing+=("AZURE_STORAGE_ACCOUNT")
[ -z "${AZURE_STORAGE_ACCOUNT_KEY:-}" ] && missing+=("AZURE_STORAGE_ACCOUNT_KEY")

if [ ${#missing[@]} -gt 0 ]; then
  cat <<'NEED'
ERROR: missing env vars: AZURE_STORAGE_ACCOUNT, AZURE_STORAGE_ACCOUNT_KEY.

Quickest setup (re-uses any existing storage account, no new infra needed):

    RG=dagster-demo-rg                # or your existing RG
    ST=<your-storage-account-name>
    export AZURE_STORAGE_ACCOUNT=$ST
    export AZURE_STORAGE_ACCOUNT_KEY=$(az storage account keys list -g "$RG" --account-name "$ST" --query "[0].value" -o tsv)
NEED
  exit 1
fi

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas azure-data-tables azure-identity
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 4 community components"
$CLI add synthetic_data_generator    --auto-install
$CLI add dataframe_to_azure_table    --auto-install
$CLI add azure_table_reader          --auto-install
$CLI add dataframe_to_csv            --auto-install

echo ">>> Writing demo defs.yaml"

cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: orders_raw
  schema_type: orders
  row_count: 100
  random_state: 42
  description: 100 synthetic e-commerce orders
  group_name: ingest
EOF

cat > "src/$PKG/defs/dataframe_to_azure_table/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_azure_table.component.DataframeToAzureTableComponent
attributes:
  asset_name: orders_in_table
  upstream_asset_key: orders_raw
  account_name_env_var: AZURE_STORAGE_ACCOUNT
  account_key_env_var: AZURE_STORAGE_ACCOUNT_KEY
  table_name: dagsterorders
  partition_key_column: customer_id
  row_key_column: order_id
  write_mode: upsert
  group_name: storage
EOF

cat > "src/$PKG/defs/azure_table_reader/defs.yaml" <<EOF
type: $PKG.components.azure_table_reader.component.AzureTableReaderComponent
attributes:
  asset_name: high_value_orders
  account_name_env_var: AZURE_STORAGE_ACCOUNT
  account_key_env_var: AZURE_STORAGE_ACCOUNT_KEY
  table_name: dagsterorders
  filter_query: "total gt 500.0"
  deps: [orders_in_table]
  group_name: read
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: high_value_orders_report
  upstream_asset_key: high_value_orders
  file_path: /tmp/azure_tables_high_value_orders.csv
  group_name: report
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Verify:
    head /tmp/azure_tables_high_value_orders.csv
    az storage entity query --table-name dagsterorders --account-name "\$AZURE_STORAGE_ACCOUNT" --account-key "\$AZURE_STORAGE_ACCOUNT_KEY" --filter "total gt 500.0" --num-results 5

Teardown:
    az storage table delete --name dagsterorders --account-name "\$AZURE_STORAGE_ACCOUNT" --account-key "\$AZURE_STORAGE_ACCOUNT_KEY"
MSG
