#!/usr/bin/env bash
# Polars-native pushdown demo — predicate-pushdown source → lazy pipeline.
#
# WHAT THIS DEMONSTRATES
#   The polars-specific components that earn their keep beyond the per-asset
#   `backend: polars` field. Pushdown only works WITHIN a single lazy query
#   graph; spread across separate Dagster assets, the asset boundary forces
#   materialization. These components solve that by either:
#     (a) pushing the predicate/projection to the source (scan_parquet,
#         read_database), or
#     (b) running multiple ops as one LazyFrame chain inside ONE asset.
#
#   Components exercised (4):
#     - synthetic_data_generator  Python-side seed (one-time)
#     - dataframe_to_parquet      land the seed as parquet on disk
#     - polars_scan_parquet       read back with PREDICATE + PROJECTION pushdown
#     - polars_pipeline           4-op LazyFrame chain (filter → with_columns →
#                                 group_by → top_n_per_group) — ONE Catalyst-style
#                                 plan executed by polars's query optimizer
#
# COST: $0 — pure local filesystem + in-memory polars.

set -euo pipefail
PROJECT_DIR="${1:-polars-pushdown-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q 'yarl<1.24'
uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q pandas pyarrow polars tabulate

CLI="uvx --from dagster-community-components-cli dagster-component"
for c in synthetic_data_generator dataframe_to_parquet \
         polars_scan_parquet polars_pipeline; do
  $CLI add $c --auto-install
done
for c in synthetic_data_generator dataframe_to_parquet \
         polars_scan_parquet polars_pipeline; do
  rm -rf "src/$PKG/defs/$c"
done

write_yaml() {
  local name="$1"; shift
  local body="$1"; shift
  mkdir -p "src/$PKG/defs/$name"
  printf "%s\n" "$body" > "src/$PKG/defs/$name/defs.yaml"
}

# 1. Synthetic orders (Python-side seed)
write_yaml "orders" "type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: orders
  schema_type: orders
  row_count: 2000
  random_state: 42
  group_name: polars_pushdown"

# 2. Land as parquet on local FS so we can scan it back with pushdown
write_yaml "orders_parquet" "type: $PKG.components.dataframe_to_parquet.component.DataframeToParquetComponent
attributes:
  asset_name: orders_parquet
  upstream_asset_key: orders
  file_path: ./orders.parquet
  group_name: polars_pushdown"

# 3. polars_scan_parquet — read with PREDICATE + PROJECTION pushdown.
#    Only rows matching predicate are read off disk; only listed columns are read.
write_yaml "paid_orders_polars" "type: $PKG.components.polars_scan_parquet.component.PolarsScanParquetComponent
attributes:
  asset_name: paid_orders_polars
  path: ./orders.parquet
  columns: [order_id, customer_id, category, total, status]
  predicate: \"status = 'paid' AND total > 50\"
  output_type: polars
  include_preview_metadata: true
  deps: [orders_parquet]
  group_name: polars_pushdown"

# 4. polars_pipeline — chain of 4 ops as ONE LazyFrame query graph.
#    Polars's planner fuses + parallelizes the whole sequence.
write_yaml "top_3_per_category_polars" "type: $PKG.components.polars_pipeline.component.PolarsPipelineComponent
attributes:
  asset_name: top_3_per_category_polars
  upstream_asset_key: paid_orders_polars
  operations:
    - op: filter
      predicate: \"total > 100\"
    - op: with_columns
      expressions:
        is_high_value: \"total > 500\"
    - op: group_by
      group_by: [category]
      aggregations:
        revenue:     {col: total, agg: sum}
        order_count: {col: order_id, agg: count}
    - op: sort
      by: [revenue]
      descending: true
    - op: head
      n: 3
  output_type: polars
  include_preview_metadata: true
  group_name: polars_pushdown"

cat <<MSG

>>> Setup complete.

    cd $PROJECT_DIR
    uv run dg check defs
    uv run dg launch --assets '*'

What you'll see:
  1. orders                       → 2000 rows in memory
  2. orders_parquet               → ./orders.parquet (Python writes)
  3. paid_orders_polars           → polars.scan_parquet() with predicate +
                                    column pushdown (only matching pages read).
                                    Look for these log lines:
                                      "projection pushdown: 5 columns"
                                      "predicate pushdown: status = 'paid' AND total > 50"
  4. top_3_per_category_polars    → polars_pipeline runs 5 ops (filter →
                                    with_columns → group_by → sort → head)
                                    as ONE lazy chain. Top-3 categories
                                    by revenue come out.

Why this matters (vs the per-asset 'backend: polars' field):
  Per-asset polars transforms get polars's execution speed, but the asset
  boundary forces materialization between steps — no predicate pushdown
  across assets. polars_scan_parquet pushes to the parquet reader.
  polars_pipeline fuses ops within one asset. These two together cover
  the optimizations the per-asset family can't.

Retargeting at production storage:
  S3:   path: s3://bucket/orders/*.parquet     + storage_options:
  GCS:  path: gs://bucket/orders/...           + storage_options:
  ADLS: path: az://container@account.dfs...    + storage_options:
MSG
