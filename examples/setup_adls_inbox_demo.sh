#!/usr/bin/env bash
# ADLS Inbox demo — sensor sees new blobs in a container, fires one run per
# new blob, asset downloads + parses the file, lands rows in SQL.
#
# This is the canonical Azure inbox-to-warehouse pattern: an upstream system
# drops files into ADLS Gen2 on its own cadence; Dagster picks them up
# automatically (no scheduler required) and processes each one as it arrives.
#
# Pipeline:
#   adls_monitor (sensor)
#         │  every 30s, lists demo/inbox/*.csv
#         │  for each new file: emits a RunRequest with container/blob in run_config
#         ▼
#   adls_to_database_asset (asset)
#         │  reads the per-run config (Dagster Config class)
#         │  downloads the blob, parses CSV/JSON/Parquet, writes to SQLite
#         ▼
#   /tmp/adls_inbox.db   (table: orders)
#
# PREREQS
#   1. Azure subscription, az CLI signed in.
#   2. ADLS Gen2 storage account + container.
#   3. AZURE_STORAGE_CONNECTION_STRING env var (for both the sensor's auth
#      AND adls_to_database_asset's blob client).
#
# REQUIRED ENV VARS
#   AZURE_STORAGE_ACCOUNT_NAME       storage account name
#   AZURE_STORAGE_CONNECTION_STRING  full connection string
#                                    (`az storage account show-connection-string ...`)
#
# COST
#   Storage:  same Standard_LRS account from the ADLS round-trip demo, <$0.05/mo.
#   Compute:  none (Dagster runs locally).
#
# TEARDOWN
#   az group delete --name dagster-demo-rg --yes

set -euo pipefail
PROJECT_DIR="${1:-adls-inbox-demo}"

missing=()
[ -z "${AZURE_STORAGE_ACCOUNT_NAME:-}" ]      && missing+=("AZURE_STORAGE_ACCOUNT_NAME")
[ -z "${AZURE_STORAGE_CONNECTION_STRING:-}" ] && missing+=("AZURE_STORAGE_CONNECTION_STRING")

if [ ${#missing[@]} -gt 0 ]; then
  cat <<NEED_CREDS
ERROR: missing required env vars: ${missing[*]}

To get the connection string for an existing storage account:
    export AZURE_STORAGE_ACCOUNT_NAME=<your-account>
    export AZURE_STORAGE_CONNECTION_STRING=\$(az storage account show-connection-string \\
        -g <resource-group> -n "\$AZURE_STORAGE_ACCOUNT_NAME" --query connectionString -o tsv)

If you don't have a storage account yet, see the ADLS Round-Trip demo:
    examples/adls_round_trip.md
NEED_CREDS
  exit 1
fi

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas pyarrow sqlalchemy azure-storage-blob azure-identity
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 3 community components"
$CLI add adls_monitor             --auto-install
$CLI add adls_to_database_asset   --auto-install
$CLI add asset_job                --auto-install

echo ">>> Writing demo defs.yaml"

# The asset that ingests one file per run. Sensor will pass container/blob via
# run_config; the asset's ADLSFileConfig consumes them.
cat > "src/$PKG/defs/adls_to_database_asset/defs.yaml" <<EOF
type: $PKG.components.adls_to_database_asset.component.ADLSToDatabaseAssetComponent
attributes:
  asset_name: orders_ingest
  connection_string_env_var: AZURE_STORAGE_CONNECTION_STRING
  database_url_env_var: DATABASE_URL
  table_name: orders
  file_format: auto
  if_exists: append
  group_name: ingest
EOF

# Define an explicit asset job that materializes just the inbox-ingest asset.
# Cleaner than relying on the auto-generated __ASSET_JOB.
cat > "src/$PKG/defs/asset_job/defs.yaml" <<EOF
type: $PKG.components.asset_job.component.AssetJobComponent
attributes:
  job_name: ingest_one_file
  asset_keys:
    - orders_ingest
  description: Materializes orders_ingest for one (container, blob) pair supplied via run_config
EOF

# Sensor that targets the explicit asset job by name + the asset's op by name
cat > "src/$PKG/defs/adls_monitor/defs.yaml" <<EOF
type: $PKG.components.adls_monitor.component.ADLSMonitorSensorComponent
attributes:
  sensor_name: adls_inbox_sensor
  storage_account_name: $AZURE_STORAGE_ACCOUNT_NAME
  container_name: demo
  directory_path: inbox/
  file_pattern: ".*\\\\.csv$"
  job_name: ingest_one_file
  target_op_name: orders_ingest
  minimum_interval_seconds: 30
  recursive: false
  connection_string_env_var: AZURE_STORAGE_CONNECTION_STRING
  default_status: stopped
EOF

cat <<MSG

>>> Setup complete.

Set up the SQLite destination + drop a sample file into the inbox:

    export DATABASE_URL=sqlite:////tmp/adls_inbox.db

    # Generate a small CSV and upload to demo/inbox/orders_2026-05-06.csv
    cat > /tmp/sample_orders.csv <<EOF2
order_id,customer_id,total
ORD0001,C001,420.50
ORD0002,C002,89.99
ORD0003,C003,1250.00
EOF2

    az storage blob upload \\
        --account-name "\$AZURE_STORAGE_ACCOUNT_NAME" \\
        --container-name demo --name inbox/orders_\$(date +%Y%m%d_%H%M%S).csv \\
        --file /tmp/sample_orders.csv \\
        --connection-string "\$AZURE_STORAGE_CONNECTION_STRING"

Now run dg dev to watch the sensor fire as soon as it sees the new file:

    cd $PROJECT_DIR
    uv run dg dev

In the UI, find sensor 'adls_inbox_sensor', enable it, and watch.
Or run it once manually:

    uv run dg sensor cursor reset adls_inbox_sensor   # starts fresh
    # then drop more files into demo/inbox/ and dg dev will pick them up

Inspect the SQLite table:
    sqlite3 /tmp/adls_inbox.db 'SELECT * FROM orders'

Teardown (storage account + RG):
    az group delete --name dagster-demo-rg --yes
MSG
