#!/usr/bin/env bash
# Local transforms + sinks demo — full DataFrame pipeline that lives entirely
# on the local filesystem. No SaaS, no cloud, no auth.
#
# WHAT THIS DEMONSTRATES
#   The transform + IO-manager + sink family of community components. Each
#   asset is materialized via a Parquet IO manager (so the data persists on
#   disk between steps), filter + summarize transforms are chained, and a
#   final sink writes Avro files for downstream consumers (Kafka, Hive, etc.)
#
# Components exercised (5 — 1 IO manager + 1 generator + 2 transforms + 1 sink):
#   - synthetic_data_generator        (upstream — generates 1000 orders)
#   - local_parquet_io_manager        (project IO manager — persists outputs to disk)
#   - filter                          (drop low-value orders)
#   - summarize                       (revenue by customer)
#   - dataframe_to_avro               (write the summary as Avro on disk)
#
# Asset graph:
#   synthetic_orders
#     → high_value_orders        (filter:   amount > 50)
#       → revenue_by_customer    (summarize: sum(amount), count(*))
#         → revenue_summary_avro (dataframe_to_avro → /tmp/revenue.avro)
#
# COST: \$0 — pure local Parquet + Avro on /tmp.

set -euo pipefail
PROJECT_DIR="${1:-local-transforms-demo}"
STORAGE_DIR="$PROJECT_ABS/local-transforms-storage"
AVRO_PATH="$PROJECT_ABS/revenue_summary.avro"

echo ">>> Clearing prior local storage"
rm -rf "$STORAGE_DIR" "$AVRO_PATH"
mkdir -p "$STORAGE_DIR"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"
PKG="$(ls src/ | head -1)"

uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q pandas pyarrow 'fastavro>=1.9.0' fsspec

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 5 components"
for c in synthetic_data_generator local_parquet_io_manager filter summarize dataframe_to_avro; do
  $CLI add $c --auto-install
done

echo ">>> Writing defs.yaml for the transform chain"

write_yaml() {
  local name="$1"; shift
  local body="$1"; shift
  mkdir -p "src/$PKG/defs/$name"
  printf "%s\n" "$body" > "src/$PKG/defs/$name/defs.yaml"
}

write_yaml "local_parquet_io_manager" "type: $PKG.components.local_parquet_io_manager.component.LocalParquetIOManagerComponent
attributes:
  resource_key: io_manager
  base_dir: $STORAGE_DIR
  create_dir: true"

write_yaml "synthetic_data_generator" "type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: synthetic_orders
  schema_type: orders
  row_count: 1000
  random_state: 42
  group_name: transforms_demo"

write_yaml "filter" "type: $PKG.components.filter.component.FilterComponent
attributes:
  asset_name: high_value_orders
  upstream_asset_key: synthetic_orders
  condition: 'total > 50'
  negate: false
  group_name: transforms_demo"

write_yaml "summarize" "type: $PKG.components.summarize.component.SummarizeComponent
attributes:
  asset_name: revenue_by_customer
  upstream_asset_key: high_value_orders
  group_by: [customer_id]
  aggregations:
    revenue: {col: total, agg: sum}
    order_count: {col: order_id, agg: count}
  group_name: transforms_demo"

write_yaml "dataframe_to_avro" "type: $PKG.components.dataframe_to_avro.component.DataframeToAvroComponent
attributes:
  asset_name: revenue_summary_avro
  upstream_asset_key: revenue_by_customer
  file_path: $AVRO_PATH
  codec: deflate
  record_name: RevenueByCustomer
  group_name: transforms_demo"

cat <<MSG

>>> Setup complete.

Validate the 5 components load:
    cd $PROJECT_DIR
    uv run dg check defs
    uv run dg list defs

Materialize the full transform chain:
    uv run dg launch --assets '*'

After materialization, inspect what landed on disk:
    ls -la $STORAGE_DIR     # Parquet snapshots of every intermediate asset
    ls -la $AVRO_PATH       # Final Avro sink output

Read the Avro back to verify:
    uv run python -c "
import fastavro
with open('$AVRO_PATH', 'rb') as f:
    for rec in fastavro.reader(f):
        print(rec)
"

Browse the asset graph:
    uv run dg dev   # http://localhost:3000 → Assets

This demo never touches a cloud bucket or warehouse — Parquet IO manager
serializes every asset to /tmp, transforms run in-process via pandas,
and the Avro sink writes locally. Swap base_dir / file_path with s3://,
gs://, az:// (via fsspec) to retarget at remote storage with the same
components.
MSG
