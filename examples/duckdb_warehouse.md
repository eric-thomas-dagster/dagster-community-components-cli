# DuckDB Warehouse demo
> ✅ **Dagster+ Serverless:** deploys as-is via `dagster-cloud serverless deploy-docker`.

A **real Dagster project** — assets persisted to a local DuckDB file via the
`duckdb_io_manager` resource, a downstream summary asset that loads the upstream
DataFrame back through the IO manager, and a daily cron schedule.

```
file_ingestion → duckdb_io_manager (resource, persists every asset)
                  → iris_summary (Python asset, loads via IO manager)
                  → cron_schedule (job + 02:00 daily)
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `file_ingestion` | ingestion | Pull the iris dataset |
| 2 | `duckdb_io_manager` | io_manager | Persists each asset materialization to a DuckDB table |
| 3 | (Python asset) | n/a | Downstream summary that loads via the IO manager |
| 4 | `cron_schedule` | infrastructure | Daily 02:00 re-materialization |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_duckdb_warehouse_demo.sh | bash
cd duckdb-warehouse-demo && uv run dg launch --assets '*'
```

## Expected output

Tables in `/tmp/iris_warehouse.duckdb`:
- `iris` — 150 rows, the source dataset
- `iris_summary` — 3 rows (one per species, with mean petal length)

```bash
duckdb /tmp/iris_warehouse.duckdb 'SELECT * FROM iris_summary'
```

## Why this demo matters

It's the IO-manager round-trip test. Most asset demos read from a source and
write to a sink, treating the in-flight DataFrame as ephemeral. This one
proves:
1. The `duckdb_io_manager` correctly persists outputs to DuckDB tables.
2. A downstream asset can load that table back as a DataFrame via the same
   IO manager (no glue Python on either end).
3. The schedule re-runs the chain — DuckDB tables get overwritten cleanly
   each day.

Validates the end-to-end IO-manager contract that other warehouse-style demos
(Snowflake, BigQuery, etc.) extrapolate from.

## See also

<!-- TODO: link related walkthroughs -->
