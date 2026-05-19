#!/usr/bin/env bash
# Apache Iceberg pipeline blueprint — runnable end-to-end demo.
# Local SQLite-backed pyiceberg catalog ('sql' catalog type) → no JVM,
# no S3, no auth. Demonstrates the iceberg_pipeline.md blueprint:
# read an existing Iceberg table → transform via a community transform
# → write the result back to another Iceberg table → read it back.
#
# Mirrors the blueprint's "another engine owns the source table" pattern
# by writing the source table once at setup, then treating it as
# pre-existing input on subsequent runs.
#
# Components exercised (5):
#   - synthetic_data_generator        seeds the "external" source table
#   - dataframe_to_iceberg_table  ×2  writes both source + downstream tables
#   - iceberg_ingestion           ×2  reads source + downstream tables
#   - summarize                       aggregation in the middle
#   - external_iceberg_table          declare-only catalog entry for lineage
#
# COST: $0 — pure local filesystem.

set -euo pipefail
PROJECT_DIR="${1:-iceberg-pipeline-demo}"
ICEBERG_WH="/tmp/iceberg-pipeline-warehouse"

echo ">>> Clearing prior local Iceberg warehouse"
rm -rf "$ICEBERG_WH"
mkdir -p "$ICEBERG_WH"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q pandas pyarrow sqlalchemy 'pyiceberg[sql-sqlite]>=0.6.0'

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 5 components"
for c in synthetic_data_generator iceberg_catalog_resource \
         dataframe_to_iceberg_table iceberg_ingestion \
         summarize external_iceberg_table; do
  $CLI add $c --auto-install
done

echo ">>> Writing defs.yaml for the Iceberg pipeline"

write_yaml() {
  local name="$1"; shift
  local body="$1"; shift
  mkdir -p "src/$PKG/defs/$name"
  printf "%s\n" "$body" > "src/$PKG/defs/$name/defs.yaml"
}

# 1. Synthetic orders (stand-in for an externally-owned source table)
write_yaml "synthetic_data_generator_orders" "type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: synthetic_orders
  schema_type: orders
  row_count: 500
  random_state: 42
  group_name: iceberg_pipeline"

# Drop the default install (avoid extra demo_customers asset)
rm -rf "src/$PKG/defs/synthetic_data_generator" 2>/dev/null || true

# 2. Shared catalog resource — SQLite-backed pyiceberg
write_yaml "iceberg_catalog_resource" "type: $PKG.components.iceberg_catalog_resource.component.IcebergCatalogResourceComponent
attributes:
  resource_key: iceberg
  catalog_type: sql
  catalog_properties:
    uri: sqlite:///$ICEBERG_WH/catalog.db
    warehouse: file://$ICEBERG_WH"

# 3. Write synthetic_orders → source Iceberg table
write_yaml "dataframe_to_iceberg_table_source" "type: $PKG.components.dataframe_to_iceberg_table.component.DataframeToIcebergTableComponent
attributes:
  asset_name: orders_iceberg_source
  upstream_asset_key: synthetic_orders
  catalog_type: sql
  catalog_properties:
    uri: sqlite:///$ICEBERG_WH/catalog.db
    warehouse: file://$ICEBERG_WH
  namespace: demo
  table_name: orders
  mode: overwrite
  group_name: iceberg_pipeline"

# 4. Declare the source table for lineage (mirrors the 'external owner' scenario)
write_yaml "external_iceberg_table" "type: $PKG.components.external_iceberg_table.component.ExternalIcebergTableAsset
attributes:
  asset_key: external/orders_source
  catalog_name: demo
  namespace: demo
  table_name: orders
  warehouse: file://$ICEBERG_WH
  catalog_type: sql
  owner_engine: external
  group_name: iceberg_pipeline"

# 5. Read source Iceberg table back into a DataFrame
write_yaml "iceberg_ingestion_orders" "type: $PKG.components.iceberg_ingestion.component.IcebergIngestionComponent
attributes:
  asset_name: orders_from_iceberg
  catalog_type: sql
  catalog_properties:
    uri: sqlite:///$ICEBERG_WH/catalog.db
    warehouse: file://$ICEBERG_WH
  namespace: demo
  table_name: orders
  deps: [orders_iceberg_source]
  group_name: iceberg_pipeline"

# 6. Transform — summarize by status
write_yaml "summarize_orders" "type: $PKG.components.summarize.component.SummarizeComponent
attributes:
  asset_name: orders_summary
  upstream_asset_key: orders_from_iceberg
  group_by: [status]
  aggregations:
    total_orders: {column: order_id, op: count}
    total_revenue: {column: amount, op: sum}
    avg_revenue: {column: amount, op: mean}
  group_name: iceberg_pipeline"

# 7. Write the summary back to Iceberg (closes the round-trip)
write_yaml "dataframe_to_iceberg_table_summary" "type: $PKG.components.dataframe_to_iceberg_table.component.DataframeToIcebergTableComponent
attributes:
  asset_name: orders_summary_iceberg
  upstream_asset_key: orders_summary
  catalog_type: sql
  catalog_properties:
    uri: sqlite:///$ICEBERG_WH/catalog.db
    warehouse: file://$ICEBERG_WH
  namespace: demo
  table_name: orders_summary
  mode: overwrite
  group_name: iceberg_pipeline"

# 8. Read the summary back via iceberg_ingestion (verification step)
write_yaml "iceberg_ingestion_summary" "type: $PKG.components.iceberg_ingestion.component.IcebergIngestionComponent
attributes:
  asset_name: orders_summary_from_iceberg
  catalog_type: sql
  catalog_properties:
    uri: sqlite:///$ICEBERG_WH/catalog.db
    warehouse: file://$ICEBERG_WH
  namespace: demo
  table_name: orders_summary
  deps: [orders_summary_iceberg]
  group_name: iceberg_pipeline"

cat <<MSG

>>> Setup complete.

Validate all components load:
    cd $PROJECT_DIR
    uv run dg check defs
    uv run dg list defs

Materialize the full Iceberg pipeline:
    uv run dg launch --assets '*'

Asset graph (open the UI):
    uv run dg dev   # http://localhost:3000

What you'll see (the blueprint flow, end-to-end):
  synthetic_orders                       → 500 rows in memory
  orders_iceberg_source                  → written to $ICEBERG_WH/demo.db/orders
  external/orders_source                 → declare-only catalog entry (lineage)
  orders_from_iceberg                    → read back via iceberg_ingestion
  orders_summary                         → aggregated by status
  orders_summary_iceberg                 → written to $ICEBERG_WH/demo.db/orders_summary
  orders_summary_from_iceberg            → read back via iceberg_ingestion

Inspect the Iceberg warehouse directly:
    ls -la $ICEBERG_WH/demo.db/orders/data/        # parquet data files
    ls -la $ICEBERG_WH/demo.db/orders/metadata/    # Iceberg manifest list + snapshots

To retarget at production:
  - Replace catalog_type: sql with rest (Polaris / Nessie / S3 Tables /
    Snowflake-managed catalog) or glue. Same components, just different
    catalog_properties.
  - warehouse: switch from file:// to s3:// / gs:// / abfs:// with cloud creds.
  - The blueprint shows the full catalog config matrix at:
    https://dagster-component-ui.vercel.app/examples/iceberg_pipeline
MSG
