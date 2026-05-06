#!/usr/bin/env bash
# Azure Cosmos DB round-trip demo.
#
# WHAT THIS DEMONSTRATES
#   Synthetic e-commerce orders → cosmosdb_writer (NoSQL/SQL API) →
#   cosmosdb_reader (queries via SQL) → dataframe_to_csv summary report.
#   Lineage flows end-to-end: writes go through Cosmos and the read-side
#   asset depends on a successful write.
#
# Pipeline (4 components):
#   synthetic_data_generator → cosmosdb_writer → cosmosdb_reader → dataframe_to_csv
#
# PREREQS
#   1. Azure subscription, az CLI signed in.
#   2. Microsoft.DocumentDB provider registered:
#        az provider register --namespace Microsoft.DocumentDB --wait
#   3. A Cosmos DB account + database + container — see "Provisioning" below.
#
# REQUIRED ENV VARS
#   COSMOS_ENDPOINT   https://<account>.documents.azure.com:443/
#   COSMOS_KEY        primary key from `az cosmosdb keys list`
#
# COST while running
#   Free tier (1 free Cosmos account per subscription): $0.
#   Without free tier: ~$24/month for the smallest provisioned-throughput
#   container; serverless would be cents/run for this volume.
#
# TEARDOWN
#   az cosmosdb delete -g dagster-demo-rg -n <account-name> --yes
#   (or full RG: az group delete --name dagster-demo-rg --yes)

set -euo pipefail
PROJECT_DIR="${1:-cosmosdb-round-trip-demo}"

missing=()
[ -z "${COSMOS_ENDPOINT:-}" ] && missing+=("COSMOS_ENDPOINT")
[ -z "${COSMOS_KEY:-}" ]      && missing+=("COSMOS_KEY")
if [ ${#missing[@]} -gt 0 ]; then
  cat <<NEED
ERROR: missing env vars: ${missing[*]}

To provision a free-tier Cosmos account:
    az group create --name dagster-demo-rg --location eastus
    az cosmosdb create -g dagster-demo-rg -n dagsterdemocosmos\$(openssl rand -hex 3) \\
        --locations regionName=eastus failoverPriority=0 \\
        --enable-free-tier true --default-consistency-level Session
    NAME=\$(az cosmosdb list -g dagster-demo-rg --query '[0].name' -o tsv)
    az cosmosdb sql database create -g dagster-demo-rg -a "\$NAME" --name demo
    az cosmosdb sql container create -g dagster-demo-rg -a "\$NAME" -d demo \\
        --name orders --partition-key-path /customer_id

    export COSMOS_ENDPOINT="https://\$NAME.documents.azure.com:443/"
    export COSMOS_KEY=\$(az cosmosdb keys list -g dagster-demo-rg -n "\$NAME" --query primaryMasterKey -o tsv)
NEED
  exit 1
fi

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas azure-cosmos
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 4 community components"
$CLI add synthetic_data_generator --auto-install
$CLI add cosmosdb_writer          --auto-install
$CLI add cosmosdb_reader          --auto-install
$CLI add dataframe_to_csv         --auto-install

echo ">>> Writing demo defs.yaml"

cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: orders_raw
  schema_type: orders
  row_count: 50
  random_state: 42
  description: 50 synthetic e-commerce orders
  group_name: ingest
EOF

cat > "src/$PKG/defs/cosmosdb_writer/defs.yaml" <<EOF
type: $PKG.components.cosmosdb_writer.component.CosmosdbWriterComponent
attributes:
  asset_name: orders_in_cosmos
  upstream_asset_key: orders_raw
  endpoint_env_var: COSMOS_ENDPOINT
  key_env_var: COSMOS_KEY
  database: demo
  container: orders
  if_exists: upsert
  id_field: order_id          # Cosmos requires 'id'; copy from order_id
  group_name: cosmos_sink
EOF

cat > "src/$PKG/defs/cosmosdb_reader/defs.yaml" <<EOF
type: $PKG.components.cosmosdb_reader.component.CosmosdbReaderComponent
attributes:
  asset_name: cosmos_high_value_orders
  endpoint_env_var: COSMOS_ENDPOINT
  key_env_var: COSMOS_KEY
  database: demo
  container: orders
  query: "SELECT * FROM c WHERE c.total > 500 ORDER BY c.total DESC"
  deps:
    - orders_in_cosmos
  group_name: cosmos_source
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: high_value_orders_report
  upstream_asset_key: cosmos_high_value_orders
  file_path: /tmp/cosmos_high_value_orders.csv
  include_index: false
  group_name: report
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Verify in Cosmos:
    az cosmosdb sql query --account-name "\$(echo \$COSMOS_ENDPOINT | sed -E 's|https://([^.]+).*|\1|')" \\
        -g dagster-demo-rg -d demo -c orders --query-text 'SELECT VALUE COUNT(1) FROM c' 2>/dev/null

Inspect the report:
    head /tmp/cosmos_high_value_orders.csv

Teardown:
    az group delete --name dagster-demo-rg --yes
MSG
