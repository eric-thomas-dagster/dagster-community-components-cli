# Mapping Data Flows: the shape

A data flow is a DAG of transformations between sources and sinks. The converter parses its script once (`dflow.py`) and then picks where it runs.

```
script ──parse──▶ flow (nodes, options, expression trees)
                     │
                     ├─▶ inline runner  (dflow.py + runtime.adf_dataflow)      any flow in the supported set
                     └─▶ SQL steps      (dflow_sql.py → warehouse_pipeline)    flows over SQL tables in one database
```

## Where a flow runs

| Flow | Runs as | Lineage | Why |
|---|---|---|---|
| SQL tables in one database, every sink `truncate: true` (or `recreate: true`), every column declared, expressions that have faithful SQL | the registry's `warehouse_pipeline`: one `sql` step per transformation, one Dagster asset per sink: `truncate_insert` keeps the table, its grants and indexes, `replace` (`recreate: true`) drops and creates it | each sink is an asset, with the pipeline's other assets around it | the database does the work, nothing is held in memory, and it is the shape the registry already maintains |
| anything else in the supported set (files, mixed stores, appends, upserts, alter row, flatten, a pivot whose values come from the data) | the inline runner in the generated project | the pipeline's own asset | exact against the documentation and the corpus, but whole tables and files sit in memory |
| everything else | a stub naming the reason | | |

The choice is made per flow, per `ExecuteDataFlow` activity, by `split._dataflow`. A flow the SQL compiler refuses (`Unsupported`) falls back to the runner, so no flow is lost by trying the component first.

## One parse, two emitters

`dflow.parse_expr` turns an expression into a tree. `_PyEmit` makes Python for the runner; `dflow_sql.Sql` makes SQL for a dialect (`mssql`, `postgres`, `snowflake`, `duckdb`, ...). Every transformation has a Python executor (`dflow._apply`) and a SQL step builder (`dflow_sql.step_sql`). Columns and their types are propagated through the flow from the sources' declared `output(...)`, which is what lets the SQL emitter decide `+` (sum or text join), `/` (fractional), conversions (a failed conversion is null, so SQL Server, Snowflake, DuckDB and Databricks use `TRY_CAST`, BigQuery `SAFE_CAST`, and dialects without one refuse) and null placement (`CASE WHEN x IS NULL` sort keys, because not every dialect has `NULLS FIRST`).

## How it is checked

`tests/test_dataflow_sql.py` runs each flow through both emitters on the same rows and compares them: filter, derive, select, aggregate, the four joins, union, conditional split (first match and disjoint), exists, lookup (all matches, first, last, `isMatch()`), rank, window, surrogate key, pivot and unpivot. The SQL is rendered by the component's own step compiler, and one test runs the component end to end on DuckDB against a file database. That proves the SQL agrees with the runner, not that either agrees with ADF; the runner's own checks are the documentation's examples and what real flows reference downstream (see `reference/mapping.md`).

## What does not go to SQL

- Appends and alter row: `warehouse_pipeline` has `truncate_insert`, `merge` (keys), `incremental` (a watermark column) and `replace`, but no plain append, and alter row has no SQL translation yet, so a sink that neither truncates nor recreates stays on the runner. Without an alter row every row is an insert, so `updateable`, `deletable` and `upsertable` change nothing and the sink maps to `truncate_insert`. A flow whose alter row upserts by key could map to `merge`; not built.
- Sort: row order inside a table is not defined, so `sort` is a pass-through; a flow that depends on it (surrogate keys or `first`/`last` after a sort) is refused for SQL only where the executor and the SQL could disagree.
- Pivot values that come from the data, flatten, alter row, `first()` and `last()`, string formats, time zones, `round`.
- Files. The registry's `polars_pipeline` and `pyspark_pipeline` read files, but their operation lists have no join, union or `sql` step, so a file flow cannot be one of them yet (wishlist ask 13).
