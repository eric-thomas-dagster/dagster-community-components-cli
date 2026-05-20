# Warehouse-native pipeline (CTAS pushdown family)

A multi-step analytics pipeline where **every transform runs as SQL inside the warehouse** — no data ever flows through Python. Each Dagster asset is one `CREATE [OR REPLACE] TABLE ... AS SELECT ...` statement; the warehouse engine plans and executes.

## When to use this pattern

- Source + sink both live in the same warehouse (Snowflake / BigQuery / Redshift / Databricks / DuckDB / Postgres)
- The compute is row-shaped (filter / group-by / window / join / union)
- You want Dagster lineage at per-step granularity, **without** materializing through pandas
- The frames are big enough that the warehouse engine is the right place to do the work

Smaller frames or chains that need Python steps → use the per-asset `backend: polars` family. Single-asset Snowflake chain with the full DataFrame API → use `snowpark_pipeline`.

## Components exercised

| Step | Component | What it emits |
|---|---|---|
| 1 | `synthetic_data_generator` | Python-side DataFrame (one-time seed) |
| 2 | `dataframe_to_table` | `raw.orders` in DuckDB (one-time load) |
| 3 | `warehouse_filter` | `paid_orders` — CTAS WHERE |
| 4 | `warehouse_top_n_per_group` | `top_3_per_category` — ROW_NUMBER() OVER PARTITION BY ... ≤ 3 |
| 5 | `warehouse_dedup` | `first_per_customer` — ROW_NUMBER() OVER PARTITION BY customer_id ORDER BY order_date DESC = 1 |
| 6 | `warehouse_union` | `paid_or_top3` — UNION DISTINCT of two derived tables |
| 7 | `warehouse_join` | `paid_with_first_order` — LEFT JOIN |
| 8 | `warehouse_summarize` | `revenue_by_status` — GROUP BY + agg |

All 6 of the `warehouse_*` components compose by chaining `output_table` → `upstream_table`. Each emits a `MaterializeResult` with the executed SQL in metadata.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_warehouse_native_pipeline_demo.sh | bash
cd warehouse-native-pipeline-demo
uv run dg check defs
uv run dg launch --assets '*'
```

## Validated end-to-end

The setup script materializes the full chain against a local DuckDB file (`./warehouse.duckdb`) — RUN_SUCCESS on the standard demo (500 input rows).

After materialization, inspect the warehouse directly:

```bash
duckdb warehouse.duckdb -c "
  .tables
  SELECT * FROM main.revenue_by_status;
  SELECT COUNT(*) AS paid_with_first_order FROM main.paid_with_first_order;
"
```

## The DuckDB-only caveat

DuckDB only supports one writer at a time. The setup script wires explicit `deps:` between the warehouse_* steps so each runs serially. This is a DuckDB constraint — production warehouses (Snowflake / BigQuery / Postgres) handle parallel writes, and you can drop the artificial deps when retargeting.

## Production retargeting

Same YAML, different `dialect:` + `database_url:`:

| Warehouse | `dialect:` | `database_url:` example |
|---|---|---|
| Snowflake | `snowflake` | `snowflake://user:pass@account/db/schema?warehouse=COMPUTE_WH` |
| BigQuery | `bigquery` | `bigquery://project/dataset` |
| Redshift | `redshift` | `redshift+psycopg2://user:pass@cluster:5439/db` |
| Databricks | `databricks` | `databricks+connector://token:dapi...@host/?http_path=...` |
| Postgres | `postgres` | `postgresql+psycopg2://user:pass@host:5432/db` |
| MSSQL | `mssql` | `mssql+pyodbc://user:pass@host/db` |
| DuckDB | `duckdb` | `duckdb:///./warehouse.duckdb` |

Each component handles dialect differences (`CREATE OR REPLACE TABLE` on dialects that support it, `DROP IF EXISTS + CREATE` on those that don't; quote style; etc.) automatically.

## When to compose vs go single-asset

This walkthrough shows **per-step lineage** — one Dagster asset per CTAS. Pros: every step has its own Dagster catalog entry; partial reruns from any step; clear column-level lineage.

When you'd switch to a single-asset chain instead:
- The whole pipeline is owned by one team and lineage at each step isn't useful
- You want the warehouse engine to plan the whole chain as one query (CTEs / subqueries) for execution-plan optimization

For that case, use `snowpark_pipeline` (Snowflake-native, full DataFrame API), `sql_transform` (Jinja-templated CTAS that can contain CTE chains), or write a single `warehouse_summarize` with a complex `upstream_table` that's itself a CTE-shaped view.
