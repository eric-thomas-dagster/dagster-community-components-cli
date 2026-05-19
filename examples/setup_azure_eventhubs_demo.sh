#!/usr/bin/env bash
# Azure Event Hubs round-trip demo.
#
# WHAT THIS DEMONSTRATES
#   100 synthetic e-commerce orders → DataFrame → published to Azure Event
#   Hubs as JSON events → consumed by eventhubs_to_database_asset → landed
#   in Azure PostgreSQL. Round-trips through a real message queue with
#   lineage flowing across the broker.
#
# Pipeline (3 components, all from the community registry):
#   synthetic_data_generator → dataframe_to_eventhub → Event Hub
#                                                          │
#                          ┌───────────────────────────────┘
#                          ▼
#                 eventhubs_to_database_asset → Azure Postgres ('orders_received')
#
# PREREQS
#   1. Azure subscription, az CLI signed in.
#   2. Microsoft.EventHub provider:  az provider register --namespace Microsoft.EventHub --wait
#   3. Microsoft.DBforPostgreSQL provider (for the consumer's DB sink).
#   4. Event Hubs namespace + a hub — see "Provisioning" below.
#   5. A Postgres Flexible Server + database (re-uses the azure_postgres demo's).
#
# REQUIRED ENV VARS
#   EVENTHUB_CONNECTION_STRING   from `az eventhubs namespace authorization-rule keys list`
#   EVENTHUB_NAME                Event Hub entity name (e.g. demo-events)
#   DATABASE_URL                 postgresql+psycopg2://...   (Azure Postgres)
#
# COST while running
#   EH Basic: ~$0.015/hr (~$11/mo) + $0.028 per million events.
#   For this demo's 100 events: fractions of a cent.
#
# TEARDOWN
#   az eventhubs namespace delete -g dagster-demo-rg -n <namespace> --yes

set -euo pipefail
PROJECT_DIR="${1:-azure-eventhubs-demo}"

missing=()
[ -z "${EVENTHUB_CONNECTION_STRING:-}" ] && missing+=("EVENTHUB_CONNECTION_STRING")
[ -z "${EVENTHUB_NAME:-}" ]              && missing+=("EVENTHUB_NAME")
[ -z "${DATABASE_URL:-}" ]               && missing+=("DATABASE_URL")

if [ ${#missing[@]} -gt 0 ]; then
  cat <<'NEED'
ERROR: missing env vars: see top of script.

To provision an Event Hubs namespace + hub:

    RG=dagster-demo-rg
    EH_NS=dgeh$(openssl rand -hex 4)
    EH_NAME=demo-events

    az group create -n "$RG" -l eastus 2>/dev/null || true
    az provider register --namespace Microsoft.EventHub --wait
    az eventhubs namespace create -g "$RG" -n "$EH_NS" -l eastus --sku Basic
    az eventhubs eventhub create -g "$RG" --namespace-name "$EH_NS" -n "$EH_NAME" \
        --partition-count 2 --cleanup-policy Delete --retention-time-in-hours 24

    export EVENTHUB_NAME=$EH_NAME
    export EVENTHUB_CONNECTION_STRING=$(az eventhubs namespace authorization-rule keys list \
        -g "$RG" --namespace-name "$EH_NS" --name RootManageSharedAccessKey \
        --query primaryConnectionString -o tsv)

For the DATABASE_URL, see setup_azure_postgres_demo.sh — this demo
re-uses the same Postgres server.
NEED
  exit 1
fi

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas sqlalchemy psycopg2-binary azure-eventhub
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 3 community components"
$CLI add synthetic_data_generator     --auto-install
$CLI add dataframe_to_eventhub        --auto-install
$CLI add eventhubs_to_database_asset  --auto-install

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

cat > "src/$PKG/defs/dataframe_to_eventhub/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_eventhub.component.DataframeToEventHubComponent
attributes:
  asset_name: orders_published_to_eventhub
  upstream_asset_key: orders_raw
  connection_string_env_var: EVENTHUB_CONNECTION_STRING
  eventhub_name: $EVENTHUB_NAME
  partition_key_column: customer_id    # group same-customer events on one partition
  batch_size: 100
  group_name: queue
EOF

cat > "src/$PKG/defs/eventhubs_to_database_asset/defs.yaml" <<EOF
type: $PKG.components.eventhubs_to_database_asset.component.EventHubsToDatabaseAssetComponent
attributes:
  asset_name: orders_consumed_from_eventhub
  deps: [orders_published_to_eventhub]   # consumer waits for producer
  connection_string_env_var: EVENTHUB_CONNECTION_STRING
  eventhub_name: $EVENTHUB_NAME
  database_url_env_var: DATABASE_URL
  table_name: orders_received
  if_exists: replace
  max_events: 200
  max_wait_seconds: 30        # poll EH for events; should drain in <5s for 100 events
  group_name: ingest
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Verify:
    uv run python -c "import pandas, sqlalchemy; \\
        eng = sqlalchemy.create_engine('\$DATABASE_URL'); \\
        print(pandas.read_sql('SELECT COUNT(*) FROM orders_received', eng))"

Teardown:
    az eventhubs namespace delete -g dagster-demo-rg -n <namespace> --yes
MSG
