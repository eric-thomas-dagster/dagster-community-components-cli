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
# Components exercised (4):
#   - synthetic_data_generator        seeds the "external" source table
#   - iceberg_catalog_resource        shared SQL catalog (no-op in this demo)
#   - dataframe_to_iceberg_table  ×2  writes both source + downstream tables
#   - iceberg_ingestion           ×2  reads source + downstream tables
#   - summarize                       aggregation in the middle
#
# COST: $0 — pure local filesystem.
# Warehouse lives inside the project dir (./iceberg-warehouse) so the demo
# works identically on macOS, Linux, and Windows (git-bash / MSYS2 / WSL).

set -euo pipefail
PROJECT_DIR="${1:-iceberg-pipeline-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

# Project-local warehouse — portable across macOS/Linux/Windows (no /tmp dependency).
# On MSYS2/git-bash (Windows), `pwd -W` returns C:/Users/... so the YAML
# embeds a real Windows path that native Python can open.
case "${OSTYPE:-}${MSYSTEM:-}" in
  *MINGW*|*msys*|*cygwin*) ICEBERG_WH="$(pwd -W 2>/dev/null || pwd)/iceberg-warehouse" ;;
  *)                       ICEBERG_WH="$(pwd)/iceberg-warehouse" ;;
esac

echo ">>> Clearing prior local Iceberg warehouse"
rm -rf "$ICEBERG_WH"
mkdir -p "$ICEBERG_WH"

# SQLite + file:// URIs differ slightly between Unix (leading /) and Windows
# (C: drive prefix). Build them so the same YAML works on both.
SQLITE_URI="sqlite:///$ICEBERG_WH/catalog.db"
case "$ICEBERG_WH" in
  /*) WAREHOUSE_URI="file://$ICEBERG_WH" ;;
  *)  WAREHOUSE_URI="file:///$ICEBERG_WH" ;;
esac

uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q pandas pyarrow sqlalchemy 'pyiceberg[sql-sqlite]>=0.6.0'

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 5 components"
for c in synthetic_data_generator iceberg_catalog_resource \
         dataframe_to_iceberg_table iceberg_ingestion summarize; do
  $CLI add $c --auto-install
done

echo ">>> Removing auto-installed default defs (we'll write our own)"
# `dagster-component add --auto-install` drops a sample defs.yaml under each
# component's directory. Our setup writes its own under distinct directory
# names (e.g. iceberg_ingestion_orders, summarize_orders), so the defaults
# would otherwise stick around referencing non-existent upstream assets like
# `sales_data` and break `dg check defs`.
for c in synthetic_data_generator dataframe_to_iceberg_table iceberg_ingestion summarize; do
  rm -rf "src/$PKG/defs/$c"
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
    uri: $SQLITE_URI
    warehouse: $WAREHOUSE_URI"

# 3. Write synthetic_orders → source Iceberg table
write_yaml "dataframe_to_iceberg_table_source" "type: $PKG.components.dataframe_to_iceberg_table.component.DataframeToIcebergTableComponent
attributes:
  asset_name: orders_iceberg_source
  upstream_asset_key: synthetic_orders
  catalog_type: sql
  catalog_properties:
    uri: $SQLITE_URI
    warehouse: $WAREHOUSE_URI
  namespace: demo
  table_name: orders
  mode: overwrite
  group_name: iceberg_pipeline"

# 4. Read source Iceberg table back into a DataFrame
#    (In production, when an upstream engine writes this table, you can drop
#    component #3 and use `external_iceberg_table` here to declare it for
#    lineage instead — see the blueprint URL at the end of this script.)
write_yaml "iceberg_ingestion_orders" "type: $PKG.components.iceberg_ingestion.component.IcebergIngestionComponent
attributes:
  asset_name: orders_from_iceberg
  catalog_type: sql
  catalog_properties:
    uri: $SQLITE_URI
    warehouse: $WAREHOUSE_URI
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
    total_orders: {col: order_id, agg: count}
    total_revenue: {col: total, agg: sum}
    avg_revenue: {col: total, agg: mean}
  group_name: iceberg_pipeline"

# 7. Write the summary back to Iceberg (closes the round-trip)
write_yaml "dataframe_to_iceberg_table_summary" "type: $PKG.components.dataframe_to_iceberg_table.component.DataframeToIcebergTableComponent
attributes:
  asset_name: orders_summary_iceberg
  upstream_asset_key: orders_summary
  catalog_type: sql
  catalog_properties:
    uri: $SQLITE_URI
    warehouse: $WAREHOUSE_URI
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
    uri: $SQLITE_URI
    warehouse: $WAREHOUSE_URI
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
  orders_from_iceberg                    → read back via iceberg_ingestion
  orders_summary                         → aggregated by status
  orders_summary_iceberg                 → written to $ICEBERG_WH/demo.db/orders_summary
  orders_summary_from_iceberg            → read back via iceberg_ingestion

Inspect the Iceberg warehouse directly:
    ls -la $ICEBERG_WH/demo.db/orders/data/        # parquet data files
    ls -la $ICEBERG_WH/demo.db/orders/metadata/    # Iceberg manifest list + snapshots

To retarget at production:
  - When another engine already writes orders_iceberg_source, drop the
    dataframe_to_iceberg_table_source component and use external_iceberg_table
    to declare the table for lineage — iceberg_ingestion_orders then reads
    from that declared key. (Mutually exclusive: own-and-write OR declare-only.)
  - Replace catalog_type: sql with rest (Polaris / Nessie / S3 Tables /
    Snowflake-managed catalog) or glue. Same components, just different
    catalog_properties.
  - warehouse: switch from file:// to s3:// / gs:// / abfs:// with cloud creds.
  - The blueprint shows the full catalog config matrix at:
    https://dagster-component-ui.vercel.app/examples/iceberg_pipeline
MSG
