#!/usr/bin/env bash
# Warehouse-native pipeline demo — pure-SQL pushdown via CTAS chain.
#
# WHAT THIS DEMONSTRATES
#   6 warehouse_* components composed into a single end-to-end pipeline.
#   No data ever flows through Python — every step is `CREATE [OR REPLACE]
#   TABLE ... AS SELECT ...` run by the warehouse engine. The "lineage"
#   in Dagster matches the CTAS lineage in the warehouse.
#
#   Components exercised (8):
#     - synthetic_data_generator     seed Python-side
#     - dataframe_to_table           load to raw.orders in DuckDB (once)
#     - warehouse_filter             CTAS WHERE — pending orders only
#     - warehouse_top_n_per_group    ROW_NUMBER() OVER (PARTITION BY ...) <= N
#     - warehouse_dedup              ROW_NUMBER() = 1 per customer_id (keep latest)
#     - warehouse_union              UNION DISTINCT of two derived tables
#     - warehouse_join               LEFT JOIN
#     - warehouse_summarize          GROUP BY + aggregations
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

echo ">>> Installing 8 components"
for c in synthetic_data_generator dataframe_to_table \
         warehouse_filter warehouse_top_n_per_group warehouse_dedup \
         warehouse_join warehouse_union warehouse_summarize; do
  $CLI add $c --auto-install
done

echo ">>> Removing auto-installed default defs (we'll write our own)"
for c in synthetic_data_generator dataframe_to_table \
         warehouse_filter warehouse_top_n_per_group warehouse_dedup \
         warehouse_join warehouse_union warehouse_summarize; do
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

# 6. warehouse_summarize — final aggregation on the join result
write_yaml "revenue_by_status" "type: $PKG.components.warehouse_summarize.component.WarehouseSummarizeComponent
attributes:
  asset_name: revenue_by_status
  database_url: duckdb:///$DB
  dialect: duckdb
  upstream_table: main.paid_with_first_order
  output_table: main.revenue_by_status
  group_by: [status]
  aggregations:
    total_orders: {col: order_id, agg: count}
    total_revenue: {col: total, agg: sum}
    avg_revenue: {col: total, agg: mean}
  mode: replace
  deps: [paid_with_first_order]
  group_name: warehouse_native"

cat <<MSG

>>> Setup complete.

    cd $PROJECT_DIR
    uv run dg check defs
    uv run dg launch --assets '*'

After the run, inspect the chain in DuckDB:
    duckdb $DB -c "
      .tables
      SELECT * FROM main.revenue_by_status;
      SELECT COUNT(*) AS paid_with_first_order FROM main.paid_with_first_order;"

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
