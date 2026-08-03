# Warehouse-native pipeline (CTAS pushdown family)

A multi-step analytics pipeline where **every transform runs as SQL inside the warehouse** — no data ever flows through Python. Each Dagster asset is one `CREATE [OR REPLACE] TABLE ... AS SELECT ...` statement; the warehouse engine plans and executes.

## When to use this pattern

- Source + sink both live in the same warehouse (Snowflake / BigQuery / Redshift / Databricks / DuckDB / Postgres)
- The compute is row-shaped (filter / group-by / window / join / union)
- You want Dagster lineage at per-step granularity, **without** materializing through pandas
- The frames are big enough that the warehouse engine is the right place to do the work

Smaller frames or chains that need Python steps → use the per-asset `backend: polars` family. Single-asset Snowflake chain with the full DataFrame API → use `snowpark_pipeline`.

## Components used

| Step | Component | What it emits |
|---|---|---|
| 1 | `synthetic_data_generator` | Python-side DataFrame (one-time seed) |
| 2 | `dataframe_to_table` | `raw.orders` in DuckDB (one-time load) |
| 3 | `warehouse_filter` | `paid_orders` — CTAS WHERE |
| 4 | `warehouse_top_n_per_group` | `top_3_per_category` — ROW_NUMBER() OVER PARTITION BY … ≤ 3 |
| 5 | `warehouse_dedup` | `first_per_customer` — ROW_NUMBER() = 1 per customer_id (keep latest) |
| 6 | `warehouse_union` | `paid_or_top3` — UNION DISTINCT of two derived tables |
| 7 | `warehouse_join` | `paid_with_first_order` — LEFT JOIN |
| 8 | `warehouse_formula` | `orders_enriched` — `net_amount` + `is_high_value` via inline SQL (Alteryx Formula In-DB equivalent) |
| 9 | `warehouse_multi_field_formula` | `orders_uppercased` — `UPPER({col})` applied to a set of columns |
| 10 | `warehouse_multi_row_formula` | `orders_running` — running totals + LAG + ROW_NUMBER via window functions |
| 11 | `warehouse_summarize` | `revenue_by_status` — GROUP BY + agg |
| 12 | `warehouse_pipeline` | `top_5_categories_pipeline` — alternative single-asset path: same logical chain compiled to ONE CTAS via CTE-WITH clauses |

The first 11 demonstrate per-step lineage (one Dagster asset per CTAS). The 12th shows the alternative — the same shape of work compiled to a single CTAS by `warehouse_pipeline`. Pick per-step when you want lineage granularity; pick `warehouse_pipeline` when the steps are tightly coupled and you want the warehouse optimizer to plan the whole chain together.

All `warehouse_*` components compose by chaining `output_table` → `upstream_table`. Each emits a `MaterializeResult` with the executed SQL in metadata.

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

## Component reference

Every component in the `warehouse_*` family shares the same six "shape" fields and adds 1–4 op-specific fields. The shared ones are listed once below; the op-specific fields follow per component.

### Shared shape fields (every warehouse_* component)

| Field | Type | Required | Description |
|---|---|---|---|
| `asset_name` | string | yes | Dagster asset name (the output table this asset produces) |
| `database_url` | string | one of | Literal SQLAlchemy URL — quick demos / DuckDB |
| `database_url_env_var` | string | one of | Env var holding the URL — production / secrets |
| `dialect` | enum | yes | `duckdb` / `postgres` / `postgresql` / `snowflake` / `bigquery` / `redshift` / `databricks` / `mssql` / `mysql` |
| `upstream_table` | string | yes | Source table name (e.g. `raw.orders`) |
| `output_table` | string | yes | Destination table name (e.g. `analytics.paid_orders`) |
| `mode` | enum | no | `replace` (CREATE OR REPLACE / DROP+CREATE) or `create_if_not_exists`. Default `replace` |
| `group_name` | string | no | Dagster asset group name |
| `deps` | list[string] | no | Upstream Dagster asset keys (lineage-only — no data passed) |
| `owners` | list[string] | no | Asset owners |
| `description` | string | no | Asset description shown in the catalog |
| `asset_tags` | dict[string, string] | no | Catalog tags |
| `kinds` | list[string] | no | Asset kinds (default: `[<dialect>, sql]`) |
| `include_preview_metadata` | bool | no | Emit a `SELECT … LIMIT N` preview after CTAS. Default `false` |
| `preview_rows` | int (1–200) | no | Rows in the preview when emitted. Default `25` |

Only the op-specific fields are listed below per component.

### warehouse_filter

CTAS WHERE predicate pushdown. The warehouse engine evaluates the WHERE clause and writes only matching rows.

| Field | Type | Required | Description |
|---|---|---|---|
| `predicate` | string | yes | SQL WHERE predicate, e.g. `"status = 'paid' AND amount > 100"` |
| `negate` | bool | no | If true, keep rows that do NOT match (becomes `NOT (predicate)`). Default `false` |

### warehouse_top_n_per_group

Top-N-per-group via `ROW_NUMBER() OVER (PARTITION BY ... ORDER BY ...) ≤ N`.

| Field | Type | Required | Description |
|---|---|---|---|
| `group_by` | list[string] | yes | Partition columns (e.g. `[category]`) |
| `sort_by` | string | yes | Column to sort by within each group (the "top" criterion) |
| `n` | int | no | Rows to keep per group. Default `3` |
| `ascending` | bool | no | If true, keep BOTTOM N. Default `false` (top N) |
| `rank_column` | string | no | Optional output column with the 1..N rank. If unset, helper column is hidden |

### warehouse_dedup

Deduplicate via `ROW_NUMBER() = 1` per `subset` key (or `SELECT DISTINCT *` when no subset given).

| Field | Type | Required | Description |
|---|---|---|---|
| `subset` | list[string] | no | Columns to dedup by. If unset, uses `SELECT DISTINCT *` |
| `order_by` | list[string] | no | Tiebreaker columns (which row to keep when subset values collide). Falls back to subset cols |
| `descending` | bool | no | If true, ORDER BY DESC (keep latest). Default `false` (keep first) |

### warehouse_join

Standard SQL join (INNER / LEFT / RIGHT / OUTER / FULL / CROSS) into a CTAS.

| Field | Type | Required | Description |
|---|---|---|---|
| `left_table` | string | yes | Left-side table name (aliased as `_l` in `select_cols`) |
| `right_table` | string | yes | Right-side table name (aliased as `_r`) |
| `how` | enum | no | `inner` / `left` / `right` / `outer` / `full` / `cross`. Default `inner` |
| `on_columns` | list[string] | one of | Join columns (same name on both sides). NB: NOT `on` — YAML 1.1 parses bare `on:` as boolean |
| `left_on` + `right_on` | list[string] | one of | When join columns have different names — must have same length |
| `select_cols` | list[string] | no | Explicit projection — use `_l.col` / `_r.col` to disambiguate. Default `_l.*, _r.*` |

### warehouse_union

Stack N tables vertically via UNION ALL (default) or UNION DISTINCT.

| Field | Type | Required | Description |
|---|---|---|---|
| `upstream_tables` | list[string] | yes | Table names to union (≥ 2). Replaces the shared `upstream_table` field |
| `distinct` | bool | no | True → UNION (drops duplicates); false → UNION ALL. Default `false` |
| `select_cols` | list[string] | no | Optional common projection when input schemas only partially overlap |

(Note: `warehouse_union` is the only `warehouse_*` component that uses `upstream_tables` instead of the singular `upstream_table`.)

### warehouse_formula

Add/replace columns via inline SQL expressions — Alteryx "Formula In-DB" equivalent.

| Field | Type | Required | Description |
|---|---|---|---|
| `expressions` | dict[string, string] | yes | Map of `output_col → SQL expression`. The expression is inlined verbatim into the SELECT clause and aliased as the output column |
| `keep_existing` | bool | no | `true` (default) → `SELECT *, <expressions>`. `false` + `keep_columns` → only those originals + new |
| `keep_columns` | list[string] | no | Explicit original-column projection when `keep_existing=false` |

Expression bodies are opaque SQL — arithmetic, `CASE`, `EXTRACT`, date funcs, JSON paths, window functions, subqueries all work.

### warehouse_multi_field_formula

Apply ONE formula template to N columns. The Alteryx "Multi-Field Formula" equivalent. `{col}` is the placeholder for each column name.

| Field | Type | Required | Description |
|---|---|---|---|
| `expression` | string | yes | Formula template using `{col}` placeholder, e.g. `"UPPER(TRIM({col}))"` |
| `columns` | list[string] | yes | Columns to apply the expression to |
| `output_mode` | enum | no | `replace` (in-place, uses `SELECT * EXCLUDE/EXCEPT` — duckdb/snowflake/bigquery/databricks only), `add_suffix` (default), `add_prefix` |
| `suffix` | string | no | Suffix for `add_suffix` mode. Default `"_calc"` |
| `prefix` | string | no | Prefix for `add_prefix` mode. Default `"calc_"` |

### warehouse_multi_row_formula

Row-relative formulas (running totals, LAG/LEAD, ranks) via SQL window functions. The Alteryx "Multi-Row Formula" equivalent. All entries in `expressions` share the same `OVER(PARTITION BY ... ORDER BY ...)` clause.

| Field | Type | Required | Description |
|---|---|---|---|
| `expressions` | dict[string, dict] | yes | Map of `output_col → {kind: <pattern>, col: <src>, ...}` |
| `partition_by` | list[string] | no | PARTITION BY columns. Omit for unpartitioned window |
| `order_by` | list[string] | no | ORDER BY columns. Required for ordered window functions (lag/lead/rank/running_*) |
| `order_descending` | list[bool] | no | Per-`order_by` descending flags. Default all ascending |

`expressions[*].kind:` supported values:

| `kind` | Required `col:` | SQL form |
|---|---|---|
| `running_total` / `running_sum` | yes | `SUM(col)` |
| `running_avg` / `running_mean` | yes | `AVG(col)` |
| `lag` | yes | `LAG(col, offset, default)` — `offset:` and `default:` optional |
| `lead` | yes | `LEAD(col, offset, default)` |
| `row_number` | no | `ROW_NUMBER()` |
| `rank` | no | `RANK()` |
| `dense_rank` | no | `DENSE_RANK()` |
| `percent_rank` | no | `PERCENT_RANK()` |
| `first_value` / `last_value` | yes | `FIRST_VALUE(col)` / `LAST_VALUE(col)` |
| `expression` | optional | Raw SQL — `{col}` substituted if both `col:` and `{col}` present |

### warehouse_summarize

GROUP BY + aggregation, CTAS-pushdown.

| Field | Type | Required | Description |
|---|---|---|---|
| `group_by` | list[string] | yes | Columns to group by |
| `aggregations` | dict[string, …] | yes | Map of `output_col → agg`. Two forms: `{out_col: agg_func}` (e.g. `revenue: sum`) or `{out_col: {col: <src>, agg: <func>}}` |

Supported `agg` values: `sum` / `mean` / `avg` / `min` / `max` / `count` / `nunique` / `n_unique`. Dialect-specific aggregates (`median`, `stddev`, percentiles) are not supported in the first cut — write a custom expression in `warehouse_formula` instead.

### warehouse_pipeline

Multi-step CTE chain compiled to ONE CTAS plan **per sink**. Two YAML shapes — both compile to the same engine; pick whichever fits the asset:

**Shape (a) — flat (one source, one ops chain, one sink):**

```yaml
source:
  upstream_table: main.raw_orders
operations:
  - {op: filter, predicate: "status = 'paid'"}
  - {op: group_by, group_by: [category],
     aggregations: {revenue: {col: total, agg: sum}}}
  - {op: top_n, sort_by: revenue, n: 5, ascending: false}
output_table: main.top_5_categories_pipeline
mode: replace
```

**Shape (b) — multi-step (multiple sources, inter-step refs, op:sql escape, multi-sink):**

```yaml
steps:
  - id: delivered_orders
    source: {kind: table, table: main.raw_orders}
    operations:
      - {op: filter, predicate: "status = 'delivered'"}

  - id: vip_customers
    source: {kind: table, table: main.raw_customers}
    operations:
      - {op: filter, predicate: "lifetime_value > 3000"}

  - id: enriched
    source: {kind: ref, ref: delivered_orders}
    operations:
      - {op: join, right: {ref: vip_customers}, on_columns: [customer_id], how: inner}
      - op: sql
        sql: "SELECT *, total * 0.15 AS commission FROM <<self>>"
      - {op: group_by, group_by: [state],
         aggregations: {revenue: {col: total, agg: sum},
                         total_commission: {col: commission, agg: sum}}}

  - id: top_states
    source: {kind: ref, ref: enriched}
    operations: [{op: top_n, sort_by: revenue, n: 3, ascending: false}]

sinks:
  - {from: enriched,   table: main.state_enriched, mode: replace}
  - {from: top_states, table: main.top_3_states,   mode: replace}
```

The `regional_top_paid_multistep` asset in this demo exercises shape (b) end-to-end against DuckDB.

| Field | Type | Required | Description |
|---|---|---|---|
| `source` | dict | shape (a) | `{upstream_table: <name>}` or `{kind: table\|sql, ...}` |
| `operations` | list[dict] | shape (a) | Ordered ops applied to `source` |
| `output_table` | string | shape (a) | Destination table |
| `mode` | string | (a) | `replace` (default) or `create_if_not_exists` |
| `steps` | list[dict] | shape (b) | Named steps. Each: `{id, source: {kind: table\|ref\|sql, ...}, operations: [...]}` |
| `sinks` | list[dict] | shape (b) | Each: `{from: <step_id>, table, mode}` |

**Source kinds inside `steps[*].source`:** `table`, `ref` (an earlier step's output), `sql` (inline SELECT becomes a seed CTE).

**The `op: sql` escape hatch.** Drop in raw SQL the DSL doesn't model. Use `<<self>>` to reference the previous CTE in this step, or `<<step_id>>` to reference any earlier step's output. Chevron syntax (not `{{ }}`) is used because Dagster pre-renders YAML through Jinja.

Supported `operations[*].op` values:

| `op` | Required params | Notes |
|---|---|---|
| `filter` | `predicate: <SQL>` | WHERE clause |
| `select` | `columns: [...]` | SELECT projection |
| `drop` | `columns: [...]` | Uses `SELECT * EXCEPT/EXCLUDE` — duckdb/bigquery/snowflake/databricks only |
| `with_columns` | `expressions: {out: <SQL expr>}` | Add computed columns |
| `group_by` | `group_by: [cols]`, `aggregations: {out: {col, agg}}` | Same shape as `warehouse_summarize` |
| `sort` | `by: [cols]`, `descending: bool/list` | ORDER BY |
| `limit` | `n: int` | LIMIT |
| `top_n` | `sort_by`, `n`, `ascending` | Global top N |
| `top_n_per_group` | `group_by`, `sort_by`, `n`, `ascending` | Top N per partition (uses SELECT * EXCEPT) |
| `dedup` | `subset` + optional `order_by` + `descending` | ROW_NUMBER() = 1 |
| `distinct` | (none) | `SELECT DISTINCT *` |
| `union` | `other: <table>` OR `{ref: <step_id>}`, `distinct: bool`, `select_cols` | UNION / UNION ALL |
| `join` | `right: <table>` OR `{ref: <step_id>}`, `how:`, `on_columns:` (or `left_on`+`right_on`), `select_cols` | Join with the running CTE chain on the left |
| `sql` | `sql: <SELECT statement>` | Escape hatch. References `<<self>>` and `<<step_id>>` |

### Component READMEs (full reference)

For everything not exercised by this demo (rarely-used fields, dialect quirks, etc.), the per-component READMEs in the templates repo are authoritative:

- [warehouse_filter](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/assets/transforms/warehouse_filter/README.md)
- [warehouse_top_n_per_group](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/assets/transforms/warehouse_top_n_per_group/README.md)
- [warehouse_dedup](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/assets/transforms/warehouse_dedup/README.md)
- [warehouse_join](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/assets/transforms/warehouse_join/README.md)
- [warehouse_union](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/assets/transforms/warehouse_union/README.md)
- [warehouse_formula](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/assets/transforms/warehouse_formula/README.md)
- [warehouse_multi_field_formula](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/assets/transforms/warehouse_multi_field_formula/README.md)
- [warehouse_multi_row_formula](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/assets/transforms/warehouse_multi_row_formula/README.md)
- [warehouse_summarize](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/assets/transforms/warehouse_summarize/README.md)
- [warehouse_pipeline](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/assets/transforms/warehouse_pipeline/README.md)

## Demo notes

- **DuckDB single-writer.** The setup chains every step with explicit `deps:` so they run serially. Production warehouses don't need this; drop the artificial deps when retargeting.
- **The CTAS is in the metadata.** Each materialized asset emits `warehouse/sql` in its asset metadata — the exact CREATE statement that ran. Click into the asset in `dg dev` to see the SQL.
- **No Python pandas in flight.** The only Python-side work is `synthetic_data_generator → dataframe_to_table` to seed the source table. Every step after that is the warehouse engine doing all the work; the Python process just submits CTAS statements and reads back row counts.
- **`mode: replace` semantics.** On DuckDB / Snowflake / BigQuery / Databricks this becomes `CREATE OR REPLACE TABLE`. On Postgres / Redshift / MSSQL / MySQL (no `CREATE OR REPLACE` support) it becomes `DROP TABLE IF EXISTS` + `CREATE TABLE AS`.
- **`SELECT * EXCEPT()` vs `EXCLUDE`.** DuckDB and Snowflake use `EXCLUDE`; BigQuery and Databricks use `EXCEPT`. `warehouse_multi_field_formula` `output_mode: replace` and `warehouse_pipeline` `op: drop` / `op: top_n_per_group` depend on this — they only work on the four dialects that support star-projection-minus-columns.

## See also

<!-- TODO: link related walkthroughs -->
