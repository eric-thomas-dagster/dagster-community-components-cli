#!/usr/bin/env bash
# Warehouse-native pipeline demo — pure-SQL pushdown via CTAS chain.
#
# WHAT THIS DEMONSTRATES
#   Every warehouse_* pushdown component composed into a single end-to-end
#   pipeline. No data ever flows through Python — every step is a CTAS run
#   by the warehouse engine.
#
#   Components exercised (12):
#     - synthetic_data_generator       seed Python-side
#     - dataframe_to_table             load to raw.orders in DuckDB (once)
#     - warehouse_filter               CTAS WHERE
#     - warehouse_top_n_per_group      ROW_NUMBER() OVER (PARTITION BY ...) <= N
#     - warehouse_dedup                ROW_NUMBER() = 1 per customer_id (keep latest)
#     - warehouse_union                UNION DISTINCT of two derived tables
#     - warehouse_join                 LEFT JOIN
#     - warehouse_formula              add columns via inline SQL (Alteryx Formula In-DB)
#     - warehouse_multi_field_formula  ONE formula template applied to N columns
#     - warehouse_multi_row_formula    running totals + LAG via window functions
#     - warehouse_summarize            GROUP BY + aggregations
#     - warehouse_pipeline             same logical chain compiled to ONE CTAS via CTE
#                                      (single-asset alt-path, side-by-side with the
#                                      per-step assets to show the trade-off)
#
#   Backend: local DuckDB file (./warehouse.duckdb). Same YAML retargets to
#   Snowflake / BigQuery / Redshift / Databricks / Postgres / MSSQL by
#   changing dialect + database_url.
#
# DUCKDB CAVEAT
#   DuckDB only supports ONE writer at a time. The asset graph is wired
#   so each warehouse_* step runs serially (explicit `deps:` between
#   them). Production warehouses (Snowflake / BigQuery / Postgres) handle
#   parallel writes; you can drop the artificial deps when retargeting.
#
# COST: $0 — pure local DuckDB.

set -euo pipefail
PROJECT_DIR="${1:-warehouse-native-pipeline-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q pandas sqlalchemy 'duckdb>=0.9.0' 'duckdb-engine>=0.10.0'

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 12 components"
for c in synthetic_data_generator dataframe_to_table \
         warehouse_filter warehouse_top_n_per_group warehouse_dedup \
         warehouse_join warehouse_union warehouse_summarize \
         warehouse_formula warehouse_multi_field_formula warehouse_multi_row_formula \
         warehouse_pipeline; do
  $CLI add $c --auto-install
done

echo ">>> Removing auto-installed default defs (we'll write our own)"
for c in synthetic_data_generator dataframe_to_table \
         warehouse_filter warehouse_top_n_per_group warehouse_dedup \
         warehouse_join warehouse_union warehouse_summarize \
         warehouse_formula warehouse_multi_field_formula warehouse_multi_row_formula \
         warehouse_pipeline; do
  rm -rf "src/$PKG/defs/$c"
done

write_yaml() {
  local name="$1"; shift
  local body="$1"; shift
  mkdir -p "src/$PKG/defs/$name"
  printf "%s\n" "$body" > "src/$PKG/defs/$name/defs.yaml"
}

DB="./warehouse.duckdb"

# Seed: Python-side synthetic orders → raw.orders in DuckDB
write_yaml "orders" "type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: orders
  schema_type: orders
  row_count: 500
  random_state: 42
  group_name: warehouse_native"

write_yaml "raw_orders_in_warehouse" "type: $PKG.components.dataframe_to_table.component.DataframeToTableComponent
attributes:
  asset_name: raw_orders_in_warehouse
  upstream_asset_key: orders
  database_url: duckdb:///$DB
  table_name: raw_orders
  if_exists: replace
  group_name: warehouse_native"

# 1. warehouse_filter — pending orders only (predicate pushdown)
write_yaml "paid_orders" "type: $PKG.components.warehouse_filter.component.WarehouseFilterComponent
attributes:
  asset_name: paid_orders
  database_url: duckdb:///$DB
  dialect: duckdb
  upstream_table: main.raw_orders
  output_table: main.paid_orders
  predicate: \"status = 'pending'\"
  mode: replace
  deps: [raw_orders_in_warehouse]
  group_name: warehouse_native"

# 2. warehouse_top_n_per_group — top 3 by total per category (ROW_NUMBER pushdown)
write_yaml "top_3_per_category" "type: $PKG.components.warehouse_top_n_per_group.component.WarehouseTopNPerGroupComponent
attributes:
  asset_name: top_3_per_category
  database_url: duckdb:///$DB
  dialect: duckdb
  upstream_table: main.raw_orders
  output_table: main.top_3_per_category
  group_by: [category]
  sort_by: total
  n: 3
  ascending: false
  rank_column: rnk
  mode: replace
  deps: [paid_orders]
  group_name: warehouse_native"

# 3. warehouse_dedup — one row per customer_id, latest by order_date
write_yaml "first_per_customer" "type: $PKG.components.warehouse_dedup.component.WarehouseDedupComponent
attributes:
  asset_name: first_per_customer
  database_url: duckdb:///$DB
  dialect: duckdb
  upstream_table: main.raw_orders
  output_table: main.first_per_customer
  subset: [customer_id]
  order_by: [order_date]
  descending: true
  mode: replace
  deps: [top_3_per_category]
  group_name: warehouse_native"

# 4. warehouse_union — combine paid_orders + top_3_per_category
write_yaml "paid_or_top3" "type: $PKG.components.warehouse_union.component.WarehouseUnionComponent
attributes:
  asset_name: paid_or_top3
  database_url: duckdb:///$DB
  dialect: duckdb
  upstream_tables: [main.paid_orders, main.top_3_per_category]
  output_table: main.paid_or_top3
  select_cols: [order_id, customer_id, category, total, status]
  distinct: true
  mode: replace
  deps: [first_per_customer]
  group_name: warehouse_native"

# 5. warehouse_join — enrich paid_orders with first_per_customer
write_yaml "paid_with_first_order" "type: $PKG.components.warehouse_join.component.WarehouseJoinComponent
attributes:
  asset_name: paid_with_first_order
  database_url: duckdb:///$DB
  dialect: duckdb
  left_table: main.paid_orders
  right_table: main.first_per_customer
  output_table: main.paid_with_first_order
  how: left
  on_columns: [customer_id]
  select_cols:
    - _l.order_id
    - _l.total
    - _l.status
    - _r.order_date AS first_order_date
  mode: replace
  deps: [paid_or_top3]
  group_name: warehouse_native"

# 6. warehouse_formula — add net_amount + is_high_value via Alteryx-style inline expressions
write_yaml "orders_enriched" "type: $PKG.components.warehouse_formula.component.WarehouseFormulaComponent
attributes:
  asset_name: orders_enriched
  database_url: duckdb:///$DB
  dialect: duckdb
  upstream_table: main.paid_with_first_order
  output_table: main.orders_enriched
  expressions:
    net_amount: \"total - 5\"
    is_high_value: \"CASE WHEN total > 500 THEN TRUE ELSE FALSE END\"
  keep_existing: true
  mode: replace
  deps: [paid_with_first_order]
  group_name: warehouse_native"

# 7. warehouse_multi_field_formula — apply UPPER() to multiple columns in one go
write_yaml "orders_uppercased" "type: $PKG.components.warehouse_multi_field_formula.component.WarehouseMultiFieldFormulaComponent
attributes:
  asset_name: orders_uppercased
  database_url: duckdb:///$DB
  dialect: duckdb
  upstream_table: main.orders_enriched
  output_table: main.orders_uppercased
  expression: \"UPPER({col})\"
  columns: [status]
  output_mode: replace
  mode: replace
  deps: [orders_enriched]
  group_name: warehouse_native"

# 8. warehouse_multi_row_formula — running totals + LAG via window functions
write_yaml "orders_running" "type: $PKG.components.warehouse_multi_row_formula.component.WarehouseMultiRowFormulaComponent
attributes:
  asset_name: orders_running
  database_url: duckdb:///$DB
  dialect: duckdb
  upstream_table: main.orders_uppercased
  output_table: main.orders_running
  partition_by: [status]
  order_by: [order_id]
  expressions:
    running_total: {kind: running_total, col: total}
    prev_total:    {kind: lag, col: total, offset: 1, default: 0}
    order_rank:    {kind: row_number}
  mode: replace
  deps: [orders_uppercased]
  group_name: warehouse_native"

# 9. warehouse_summarize — per-step summarize before the pipeline closeout
write_yaml "revenue_by_status" "type: $PKG.components.warehouse_summarize.component.WarehouseSummarizeComponent
attributes:
  asset_name: revenue_by_status
  database_url: duckdb:///$DB
  dialect: duckdb
  upstream_table: main.orders_running
  output_table: main.revenue_by_status
  group_by: [status]
  aggregations:
    total_orders: {col: order_id, agg: count}
    total_revenue: {col: total, agg: sum}
    avg_revenue: {col: total, agg: mean}
  mode: replace
  deps: [orders_running]
  group_name: warehouse_native"

# 10. warehouse_pipeline — same logical work compiled to ONE CTAS via CTE chain
#     (alternative path; shows the trade-off vs per-step lineage)
write_yaml "top_5_categories_pipeline" "type: $PKG.components.warehouse_pipeline.component.WarehousePipelineComponent
attributes:
  asset_name: top_5_categories_pipeline
  database_url: duckdb:///$DB
  dialect: duckdb
  source:
    upstream_table: main.raw_orders
  operations:
    - op: filter
      predicate: \"status = 'paid'\"
    - op: group_by
      group_by: [category]
      aggregations:
        revenue:     {col: total,    agg: sum}
        order_count: {col: order_id, agg: count}
    - op: top_n
      sort_by: revenue
      n: 5
      ascending: false
  output_table: main.top_5_categories_pipeline
  mode: replace
  deps: [revenue_by_status]
  group_name: warehouse_native"

# ── 11. warehouse_pipeline (MULTI-STEP shape) — multiple sources, ref between
#       steps, op:sql escape hatch, multi-sink. Same component as #10, but
#       exercising the YAML form that lets you write stored-procedure-style
#       pipelines: join two filtered streams, drop into raw SQL for a
#       commission calc the DSL doesn't model, group + sort + fan out to two
#       output tables. Compiles to ONE CTE-CTAS plan per sink.

# Seed: synthetic customers → main.raw_customers
# Pin customers gen AFTER the whole warehouse_* chain so its DB write doesn't
# race with the per-step writers on DuckDB's single-writer constraint. Chains
# off top_5_categories_pipeline (the existing tail of the chain).
write_yaml "customers" "type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: customers
  schema_type: customers
  row_count: 200
  random_state: 7
  deps: [top_5_categories_pipeline]
  group_name: warehouse_native"

write_yaml "raw_customers_in_warehouse" "type: $PKG.components.dataframe_to_table.component.DataframeToTableComponent
attributes:
  asset_name: raw_customers_in_warehouse
  upstream_asset_key: customers
  database_url: duckdb:///$DB
  table_name: raw_customers
  if_exists: replace
  group_name: warehouse_native"

write_yaml "regional_top_paid_multistep" "type: $PKG.components.warehouse_pipeline.component.WarehousePipelineComponent
attributes:
  asset_name: regional_top_paid_multistep
  database_url: duckdb:///$DB
  dialect: duckdb
  steps:
    - id: delivered_orders
      source: {kind: table, table: main.raw_orders}
      operations:
        - {op: filter, predicate: \"status = 'delivered'\"}

    - id: vip_customers
      source: {kind: table, table: main.raw_customers}
      operations:
        - {op: filter, predicate: \"lifetime_value > 3000\"}

    - id: enriched
      source: {kind: ref, ref: delivered_orders}
      operations:
        - op: join
          right: {ref: vip_customers}
          on_columns: [customer_id]
          how: inner
        - op: sql
          sql: |
            SELECT *, total * 0.15 AS commission
            FROM <<self>>
        - op: group_by
          group_by: [state]
          aggregations:
            revenue:          {col: total,      agg: sum}
            total_commission: {col: commission, agg: sum}
            order_count:      {col: order_id,   agg: count}

    - id: top_states
      source: {kind: ref, ref: enriched}
      operations:
        - {op: top_n, sort_by: revenue, n: 3, ascending: false}

  sinks:
    - {from: enriched,    table: main.state_enriched, mode: replace}
    - {from: top_states,  table: main.top_3_states,   mode: replace}

  deps: [raw_orders_in_warehouse, raw_customers_in_warehouse, top_5_categories_pipeline]
  group_name: warehouse_native
  include_preview_metadata: true"

cat <<MSG

>>> Setup complete.

    cd $PROJECT_DIR
    uv run dg check defs
    uv run dg launch --assets '*'

After the run, inspect the chain in DuckDB:
    duckdb $DB -c "
      .tables
      SELECT * FROM main.revenue_by_status;
      SELECT * FROM main.top_5_categories_pipeline;
      SELECT * FROM main.state_enriched ORDER BY revenue DESC;
      SELECT * FROM main.top_3_states   ORDER BY revenue DESC;
      SELECT COUNT(*) AS rows_in_orders_running FROM main.orders_running;"

What's notable about regional_top_paid_multistep:
  • TWO sources (orders + customers) — both filtered independently
  • Inter-step JOIN via {ref: vip_customers}
  • Raw-SQL escape hatch (op: sql) for the commission calc — anything
    the DSL doesn't model, just write the SQL. <<self>> refers to the
    previous CTE in this step; <<step_id>> works for cross-step refs.
  • MULTI-SINK — one asset writes BOTH 'state_enriched' (full set) AND
    'top_3_states' (the top-N), each as its own CTE-CTAS, both seeing
    the same WITH clause so the optimizer reasons about the whole graph.

Production retargeting (no code changes):
  - Snowflake:  dialect: snowflake  + database_url: snowflake://user:pass@account/db/schema?warehouse=COMPUTE_WH
  - BigQuery:   dialect: bigquery   + database_url: bigquery://project/dataset
  - Redshift:   dialect: redshift   + database_url: redshift+psycopg2://user:pass@cluster:5439/db
  - Databricks: dialect: databricks + database_url: databricks+connector://token:dapi...@host/?http_path=...
  - Postgres:   dialect: postgres   + database_url: postgresql+psycopg2://user:pass@host:5432/db

  Drop the artificial deps between warehouse_* steps when retargeting —
  production warehouses handle parallel writes; DuckDB's single-writer
  was the only reason for the chain.
MSG
