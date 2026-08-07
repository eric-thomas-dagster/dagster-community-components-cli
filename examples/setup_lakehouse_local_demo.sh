#!/usr/bin/env bash
# Lakehouse local roundtrip — full Iceberg + Delta read/write cycle against
# the local filesystem. No cloud, no auth, no JVM, no catalog server.
#
# WHAT THIS DEMONSTRATES
#   The Iceberg + Delta family of community components — *_ingestion (read),
#   dataframe_to_*_table (write), iceberg_catalog_resource (shared catalog),
#   external_*_table (declare-only) — all wired into a single end-to-end demo.
#   Generates synthetic orders + events, writes them to local Iceberg and
#   Delta tables, then reads them back via the ingestion components.
#
# Iceberg side — SQLite-backed catalog at /tmp/iceberg-warehouse/catalog.db
# (pyiceberg's 'sql' catalog type — no JVM, no server, no auth):
#   synthetic_orders (generator)
#     → orders_to_iceberg (dataframe_to_iceberg_table → /tmp/iceberg-warehouse/demo.db/orders)
#       → orders_from_iceberg (iceberg_ingestion reads same table back)
#   external_iceberg_orders (declare-only AssetSpec, points at same table)
#
# Delta side — local filesystem path /tmp/delta-events:
#   synthetic_events (generator)
#     → events_to_delta (dataframe_to_delta_table → /tmp/delta-events)
#       → events_from_delta (delta_ingestion reads same table back)
#   external_delta_events (declare-only AssetSpec)
#
# Components exercised (7):
#   - synthetic_data_generator (x2)       (already validated — used as upstream)
#   - iceberg_catalog_resource             (shared catalog config)
#   - dataframe_to_iceberg_table           (write)
#   - iceberg_ingestion                    (read)
#   - dataframe_to_delta_table             (write)
#   - delta_ingestion                      (read)
#   - external_iceberg_table               (declare-only)
#   - external_delta_table                 (declare-only)
#
# COST: \$0 — pure local filesystem.

set -euo pipefail
PROJECT_DIR="${1:-lakehouse-local-demo}"
ICEBERG_WH="$PROJECT_ABS/iceberg-warehouse"
DELTA_PATH="$PROJECT_ABS/delta-events"

echo ">>> Clearing prior local lakehouse storage"
rm -rf "$ICEBERG_WH" "$DELTA_PATH"
mkdir -p "$ICEBERG_WH"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"
PKG="$(ls src/ | head -1)"

uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q pandas pyarrow sqlalchemy 'pyiceberg[sql-sqlite]>=0.6.0' 'deltalake>=0.15.0'

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 8 lakehouse components"
for c in synthetic_data_generator iceberg_catalog_resource \
         dataframe_to_iceberg_table iceberg_ingestion \
         dataframe_to_delta_table delta_ingestion \
         external_iceberg_table external_delta_table; do
  $CLI add $c --auto-install
done

echo ">>> Writing defs.yaml for the lakehouse cycle"

write_yaml() {
  local name="$1"; shift
  local body="$1"; shift
  mkdir -p "src/$PKG/defs/$name"
  printf "%s\n" "$body" > "src/$PKG/defs/$name/defs.yaml"
}

# Two upstream generators
write_yaml "synthetic_data_generator_orders" "type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: synthetic_orders
  schema_type: orders
  row_count: 200
  random_state: 42
  group_name: lakehouse_demo"

mkdir -p "src/$PKG/defs/synthetic_data_generator_events"
cat > "src/$PKG/defs/synthetic_data_generator_events/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: synthetic_events
  schema_type: events
  row_count: 200
  random_state: 7
  group_name: lakehouse_demo
EOF
# The generator's auto-installed example uses the default schema; overwrite it
# so we don't end up with a third `demo_customers` asset.
rm -f "src/$PKG/defs/synthetic_data_generator/defs.yaml"
rmdir "src/$PKG/defs/synthetic_data_generator" 2>/dev/null || true

# Iceberg catalog resource (hadoop = filesystem; no server needed)
write_yaml "iceberg_catalog_resource" "type: $PKG.components.iceberg_catalog_resource.component.IcebergCatalogResourceComponent
attributes:
  resource_key: iceberg
  catalog_type: sql
  catalog_properties:
    uri: sqlite:///$ICEBERG_WH/catalog.db
    warehouse: file://$ICEBERG_WH"

# Write synthetic_orders → local Iceberg table
write_yaml "dataframe_to_iceberg_table" "type: $PKG.components.dataframe_to_iceberg_table.component.DataframeToIcebergTableComponent
attributes:
  asset_name: orders_to_iceberg
  upstream_asset_key: synthetic_orders
  catalog_type: sql
  catalog_properties:
    uri: sqlite:///$ICEBERG_WH/catalog.db
    warehouse: file://$ICEBERG_WH
  namespace: demo
  table_name: orders
  mode: overwrite
  group_name: lakehouse_demo"

# Read it back via iceberg_ingestion
write_yaml "iceberg_ingestion" "type: $PKG.components.iceberg_ingestion.component.IcebergIngestionComponent
attributes:
  asset_name: orders_from_iceberg
  catalog_type: sql
  catalog_properties:
    uri: sqlite:///$ICEBERG_WH/catalog.db
    warehouse: file://$ICEBERG_WH
  namespace: demo
  table_name: orders
  deps: [orders_to_iceberg]
  group_name: lakehouse_demo"

# Write synthetic_events → local Delta table
write_yaml "dataframe_to_delta_table" "type: $PKG.components.dataframe_to_delta_table.component.DataframeToDeltaTableComponent
attributes:
  asset_name: events_to_delta
  upstream_asset_key: synthetic_events
  table_uri: $DELTA_PATH
  mode: overwrite
  group_name: lakehouse_demo"

# Read it back via delta_ingestion
write_yaml "delta_ingestion" "type: $PKG.components.delta_ingestion.component.DeltaIngestionComponent
attributes:
  asset_name: events_from_delta
  table_uri: $DELTA_PATH
  deps: [events_to_delta]
  group_name: lakehouse_demo"

# Declare-only side: external_iceberg_table + external_delta_table
write_yaml "external_iceberg_table" "type: $PKG.components.external_iceberg_table.component.ExternalIcebergTableAsset
attributes:
  asset_key: external/iceberg_orders
  catalog_name: demo
  namespace: demo
  table_name: orders
  warehouse: file://$ICEBERG_WH
  catalog_type: sql
  owner_engine: dagster
  group_name: lakehouse_demo"

write_yaml "external_delta_table" "type: $PKG.components.external_delta_table.component.ExternalDeltaTableAsset
attributes:
  asset_key: external/delta_events
  table_uri: $DELTA_PATH
  owner_engine: dagster
  group_name: lakehouse_demo"

cat <<MSG

>>> Setup complete.

Validate all components load:
    cd $PROJECT_DIR
    uv run dg check defs
    uv run dg list defs

Materialize the full lakehouse cycle:
    uv run dg launch --assets '*'

What you'll see:
  - synthetic_orders / synthetic_events    generated in memory (pandas)
  - orders_to_iceberg / events_to_delta    written to local files
                                             $ICEBERG_WH
                                             $DELTA_PATH
  - orders_from_iceberg / events_from_delta read back via the ingestion components
  - external/iceberg_orders, external/delta_events
                                           declared in the catalog (declare-only)

Browse the asset graph:
    uv run dg dev   # http://localhost:3000 → Assets graph

To retarget at production:
  - Iceberg: switch catalog_type from 'sql' to 'rest' (Polaris/Nessie/S3 Tables/
    Snowflake-managed catalog) or 'glue'. Same component, different catalog_properties.
  - Delta:   switch table_uri from /tmp/... to s3://, az://, gs://, or uc://. Add
    storage_options for cloud credentials.
MSG
