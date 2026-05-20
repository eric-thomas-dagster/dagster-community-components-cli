#!/usr/bin/env bash
# polars_pipeline demo — multi-step LazyFrame chain in a single Dagster asset.
#
# Runs 4 polars ops (filter → group_by → sort → head_per_group) as ONE
# Catalyst-style lazy chain. The polars query planner fuses + parallelizes
# the whole sequence; the asset boundary doesn't break the optimization
# the way per-asset transforms do.
#
# Components exercised (2):
#   - synthetic_data_generator  (seed)
#   - polars_pipeline           (multi-op chain in one asset)
#
# COST: $0 — pure in-memory polars

set -euo pipefail
PROJECT_DIR="${1:-polars-pipeline-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q 'yarl<1.24'
uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q pandas pyarrow polars tabulate

CLI="uvx --from dagster-community-components-cli dagster-component"
for c in synthetic_data_generator polars_pipeline; do
  $CLI add $c --auto-install
done
for c in synthetic_data_generator polars_pipeline; do
  rm -rf "src/$PKG/defs/$c"
done

write_yaml() {
  local name="$1"; shift
  local body="$1"; shift
  mkdir -p "src/$PKG/defs/$name"
  printf "%s\n" "$body" > "src/$PKG/defs/$name/defs.yaml"
}

write_yaml "orders" "type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: orders
  schema_type: orders
  row_count: 1000
  random_state: 42
  group_name: polars_pipeline_demo"

write_yaml "top_status_per_category" "type: $PKG.components.polars_pipeline.component.PolarsPipelineComponent
attributes:
  asset_name: top_status_per_category
  upstream_asset_key: orders
  operations:
    - op: filter
      predicate: \"total > 100\"
    - op: group_by
      group_by: [category, status]
      aggregations:
        revenue:     {col: total, agg: sum}
        order_count: {col: order_id, agg: count}
    - op: sort
      by: [category, revenue]
      descending: [false, true]
    - op: head_per_group
      group_by: [category]
      n: 2
  output_type: polars
  include_preview_metadata: true
  group_name: polars_pipeline_demo"

cat <<MSG

>>> Setup complete.

    cd $PROJECT_DIR
    uv run dg check defs
    uv run dg launch --assets '*'

What you'll see: 1000 input rows → polars's query planner fuses the 4
ops into one lazy chain → output is the top-2 (status, revenue) pairs
per product category. RUN_SUCCESS in well under a second.
MSG
