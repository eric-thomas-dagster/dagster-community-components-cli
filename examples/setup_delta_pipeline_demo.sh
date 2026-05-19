#!/usr/bin/env bash
# Delta Lake pipeline blueprint — runnable end-to-end demo.
# Uses delta-rs (the Rust impl of the Delta protocol) — no Spark, no JVM.
# All storage is local filesystem at /tmp/delta-pipeline-warehouse.
# Demonstrates the delta_pipeline.md blueprint: read existing Delta →
# transform → write back to another Delta table → read it back.
#
# Mirrors the blueprint's "another engine owns the source table" pattern
# by writing the source table once at setup, then treating it as
# pre-existing input on subsequent runs.
#
# Components exercised (4):
#   - synthetic_data_generator        seeds the "external" source table
#   - dataframe_to_delta_table  ×2    writes source + downstream tables
#   - delta_ingestion           ×2    reads source + downstream tables
#   - summarize                       aggregation in the middle
#   - external_delta_table            declare-only catalog entry for lineage
#
# COST: $0 — pure local filesystem.

set -euo pipefail
PROJECT_DIR="${1:-delta-pipeline-demo}"
DELTA_WH="/tmp/delta-pipeline-warehouse"
DELTA_SRC="$DELTA_WH/orders"
DELTA_SUM="$DELTA_WH/orders_summary"

echo ">>> Clearing prior local Delta warehouse"
rm -rf "$DELTA_WH"
mkdir -p "$DELTA_WH"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q pandas pyarrow 'deltalake>=0.15.0'

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 4 components"
for c in synthetic_data_generator dataframe_to_delta_table \
         delta_ingestion summarize external_delta_table; do
  $CLI add $c --auto-install
done

echo ">>> Writing defs.yaml for the Delta pipeline"

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
  group_name: delta_pipeline"

rm -rf "src/$PKG/defs/synthetic_data_generator" 2>/dev/null || true

# 2. Write synthetic_orders → source Delta table
write_yaml "dataframe_to_delta_table_source" "type: $PKG.components.dataframe_to_delta_table.component.DataframeToDeltaTableComponent
attributes:
  asset_name: orders_delta_source
  upstream_asset_key: synthetic_orders
  table_uri: $DELTA_SRC
  mode: overwrite
  group_name: delta_pipeline"

# 3. Declare the source table for lineage (mirrors 'external owner' scenario)
write_yaml "external_delta_table" "type: $PKG.components.external_delta_table.component.ExternalDeltaTableAsset
attributes:
  asset_key: external/orders_source
  table_uri: $DELTA_SRC
  owner_engine: external
  group_name: delta_pipeline"

# 4. Read source Delta table back into a DataFrame
write_yaml "delta_ingestion_orders" "type: $PKG.components.delta_ingestion.component.DeltaIngestionComponent
attributes:
  asset_name: orders_from_delta
  table_uri: $DELTA_SRC
  deps: [orders_delta_source]
  group_name: delta_pipeline"

# 5. Transform — summarize by status
write_yaml "summarize_orders" "type: $PKG.components.summarize.component.SummarizeComponent
attributes:
  asset_name: orders_summary
  upstream_asset_key: orders_from_delta
  group_by: [status]
  aggregations:
    total_orders: {column: order_id, op: count}
    total_revenue: {column: amount, op: sum}
    avg_revenue: {column: amount, op: mean}
  group_name: delta_pipeline"

# 6. Write the summary back to Delta (round-trip complete)
write_yaml "dataframe_to_delta_table_summary" "type: $PKG.components.dataframe_to_delta_table.component.DataframeToDeltaTableComponent
attributes:
  asset_name: orders_summary_delta
  upstream_asset_key: orders_summary
  table_uri: $DELTA_SUM
  mode: overwrite
  group_name: delta_pipeline"

# 7. Read the summary back via delta_ingestion (verification step)
write_yaml "delta_ingestion_summary" "type: $PKG.components.delta_ingestion.component.DeltaIngestionComponent
attributes:
  asset_name: orders_summary_from_delta
  table_uri: $DELTA_SUM
  deps: [orders_summary_delta]
  group_name: delta_pipeline"

cat <<MSG

>>> Setup complete.

Validate all components load:
    cd $PROJECT_DIR
    uv run dg check defs
    uv run dg list defs

Materialize the full Delta pipeline:
    uv run dg launch --assets '*'

Asset graph (open the UI):
    uv run dg dev   # http://localhost:3000

What you'll see:
  synthetic_orders                       → 500 rows in memory
  orders_delta_source                    → Parquet + _delta_log at $DELTA_SRC
  external/orders_source                 → declare-only catalog entry (lineage)
  orders_from_delta                      → read back via delta_ingestion
  orders_summary                         → aggregated by status
  orders_summary_delta                   → written to $DELTA_SUM
  orders_summary_from_delta              → read back via delta_ingestion

Inspect the Delta tables directly:
    ls -la $DELTA_SRC/_delta_log/       # Delta protocol commit log
    ls -la $DELTA_SRC/                  # Parquet data files
    cat $DELTA_SRC/_delta_log/00000000000000000000.json

To retarget at production storage:
  - S3:           table_uri: s3://bucket/path     + storage_options for AWS creds
  - ADLS Gen2:    table_uri: az://container@account.dfs.core.windows.net/path
  - GCS:          table_uri: gs://bucket/path     + GOOGLE_SERVICE_ACCOUNT_KEY
  - Unity Catalog: table_uri: uc://catalog.schema.table  + UC_TOKEN
  Same components, just different table_uri + storage_options. See the
  blueprint at https://dagster-component-ui.vercel.app/examples/delta_pipeline
MSG
