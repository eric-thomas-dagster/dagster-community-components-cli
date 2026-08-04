# Recurring SQL→SQL replication — Postgres → DuckDB end-to-end (Oracle / Db2 / Snowflake / BigQuery retargets unchanged)
> ❌ **Dagster+ Serverless:** local-only demo — requires container/server dependency.

> **Replication vs. migration:** this walkthrough covers the **recurring** data-sync pattern — runs on a schedule, picks up incrementally, keeps the warehouse in sync with the operational DB. For the **one-time** warehouse-migration story (PL/SQL procedures, scheduled jobs, views, triggers — everything that's *not* a table), see [warehouse_migration.md](warehouse_migration.md), which uses `database_schema_inventory` alongside this same `database_replication` component.

Replicate tables from a legacy operational database into a modern warehouse using the dedicated `database_replication` community component (Sling under the hood — no Sling YAML to write, no Python in memory, no Dagster framework knobs to twist).

Same component + same YAML retargets across:

- **Sources:** Postgres / MySQL / MSSQL / **Oracle** / **Db2** / Snowflake / BigQuery / Redshift / Databricks / DuckDB / ClickHouse / MariaDB / SQLite
- **Targets:** Snowflake / BigQuery / Redshift / Databricks / DuckDB / Postgres / MySQL / MSSQL / Oracle / Db2 / ClickHouse / MariaDB / SQLite

Only `source_type` / `target_type` and the connection URLs change.

## Components used

| Component | Source | Role |
|---|---|---|
| `database_replication` | community (new) | Streaming SQL→SQL replication via Sling. 3 instances in this demo (full refresh, incremental, filtered subset). |

That's it — one component, three YAML files. Sling is bundled in `dagster-sling` so no separate install.

## Architecture

```
   Postgres (legacy_app)                DuckDB (warehouse stand-in)
   ┌─────────────────────────┐          ┌──────────────────────────┐
   │ app.customers (50 rows) │  full    │ raw.customers (50)       │
   │                         │ ───────► │                          │
   ├─────────────────────────┤  incr.   ├──────────────────────────┤
   │ app.orders (200 rows)   │ ───────► │ raw.orders (200, +deltas)│
   │   updated_at, order_id  │  by      │                          │
   │                         │  upsert  │                          │
   ├─────────────────────────┤  filter  ├──────────────────────────┤
   │ app.orders (region=EU)  │ ───────► │ raw.orders_eu (50)       │
   │ 5 selected columns      │  + cols  │                          │
   └─────────────────────────┘          └──────────────────────────┘
                          │                          ▲
                          │  Sling (streaming,       │
                          │  no pandas in memory)    │
                          └──────────────────────────┘
```

`deps:` chains the three instances (customers → orders → orders_eu) — sensible dim-then-fact-then-view ordering and avoids DuckDB single-writer contention. With a real warehouse (Snowflake / BigQuery / etc.), the chain is optional — those handle concurrent writes natively.

## Run

```bash
bash setup_replication_demo.sh
cd replication-demo
source .env.demo

uv run dg check defs
uv run dg launch --assets '*'
```

Then verify:

```bash
duckdb /tmp/replication-warehouse.duckdb -c "
SELECT 'customers' AS tbl, COUNT(*) FROM raw.customers
UNION ALL SELECT 'orders', COUNT(*) FROM raw.orders
UNION ALL SELECT 'orders_eu', COUNT(*) FROM raw.orders_eu;"
# customers=50, orders=200, orders_eu=50
```

Cleanup: `docker rm -f dg-replication-postgres && rm -f /tmp/replication-warehouse.duckdb`.

## Try the incremental path

Add 10 new orders to the source, then re-run just the orders asset — only the delta moves:

```bash
docker exec dg-replication-postgres psql -U postgres -d legacy_app -c \
  "INSERT INTO app.orders SELECT 200+i, i, (random()*500+10)::NUMERIC(10,2),
   'paid', 'US', NOW(), NOW() FROM generate_series(1,10) i;"

uv run dg launch --assets orders_warehouse
# Sling logs: "inserted 10 rows into raw.orders" — not 210
```

The `incremental_column: updated_at` + `primary_key: [order_id]` config means Sling keeps its own watermark per stream and upserts by primary key — no manual watermark management on your side.

## The three YAML shapes

```yaml
# 1. Full refresh — small slowly-changing dim table
type: dagster_component_templates.DatabaseReplicationComponent
attributes:
  asset_name: customers_warehouse
  source_connection_env_var: SOURCE_DB_URL
  source_type: postgres
  source_table: app.customers
  target_connection_env_var: TARGET_DB_URL
  target_type: duckdb
  target_table: raw.customers
  mode: full_refresh
```

```yaml
# 2. Incremental upsert — big append-mostly fact table
type: dagster_component_templates.DatabaseReplicationComponent
attributes:
  asset_name: orders_warehouse
  source_connection_env_var: SOURCE_DB_URL
  source_type: postgres
  source_table: app.orders
  target_connection_env_var: TARGET_DB_URL
  target_type: duckdb
  target_table: raw.orders
  mode: incremental
  incremental_column: updated_at      # Sling watermark column
  primary_key: [order_id]             # for upsert behavior
  deps: [customers_warehouse]
```

```yaml
# 3. Column subset + WHERE filter — derived/regional view
type: dagster_component_templates.DatabaseReplicationComponent
attributes:
  asset_name: orders_eu_warehouse
  source_connection_env_var: SOURCE_DB_URL
  source_type: postgres
  source_table: app.orders
  target_connection_env_var: TARGET_DB_URL
  target_type: duckdb
  target_table: raw.orders_eu
  mode: full_refresh
  select_columns: [order_id, customer_id, amount, status, created_at]
  where_clause: "region = 'EU'"
  deps: [orders_warehouse]
```

## Production retargeting

Change two YAML fields, change two env vars, ship it. (This is also exactly what a one-time warehouse migration looks like from the data-side — see [warehouse_migration.md](warehouse_migration.md) for the broader story including PL/SQL, scheduled jobs, and views.)

### Oracle → Snowflake (the canonical migration)

```yaml
attributes:
  source_type: oracle
  target_type: snowflake
  # everything else unchanged
```

```bash
export SOURCE_DB_URL='oracle://app_user:app_pass@oracledb-prod:1521/?service_name=ORCL'
export TARGET_DB_URL='snowflake://etl_user:****@account.snowflakecomputing.com/RAW_DB/PUBLIC?warehouse=LOAD_WH&role=LOADER'
```

### Db2 → Snowflake

```yaml
attributes:
  source_type: db2
  target_type: snowflake
```

```bash
export SOURCE_DB_URL='db2://db2inst1:****@db2-mainframe:50000/PROD'
export TARGET_DB_URL='snowflake://...'
```

### Postgres → BigQuery

```yaml
attributes:
  source_type: postgres
  target_type: bigquery
```

```bash
export TARGET_DB_URL='bigquery://my-project/analytics_dataset?keyfile=/etc/sa-loader.json'
```

### Postgres → Redshift

```yaml
attributes:
  target_type: redshift
```

```bash
export TARGET_DB_URL='redshift://etl_user:****@cluster.us-east-1.redshift.amazonaws.com:5439/prod'
```

### Postgres → Databricks SQL

```yaml
attributes:
  target_type: databricks
```

```bash
export TARGET_DB_URL='databricks://token:dapi****@dbc-x.cloud.databricks.com/?http_path=/sql/1.0/warehouses/xxxxxxxx'
```

## Why a dedicated component instead of `dagster-sling` directly

The official `dagster-sling` integration is excellent — and this component uses it under the hood (`@sling_assets`). What this dedicated component adds:

| Direct `dagster-sling` | `database_replication` |
|---|---|
| You write the Sling replication YAML by hand | You set 7 fields; the spec is generated |
| You configure `SlingResource` + `SlingConnectionResource` separately | One asset = one resource = one YAML |
| Multi-stream replication file = one big asset | Each table = its own asset (better lineage in the UI) |
| Sling's mode names (`full-refresh`, `truncate`) | Dagster-flavored names (`full_refresh`, `truncate`) |
| Multiple replications collide on the `sling` resource key | Each instance gets `sling_<asset_name>` — multiple per project |

If you outgrow the dedicated component (e.g. you want one replication-file orchestrating 100 tables with shared connections), drop down to `dagster-sling` directly — same engine, fuller API surface.

## Demo notes

- **First-run network call.** Sling fetches its bundled DuckDB CLI binary from GitHub the first time you target DuckDB. After that it's cached in `~/.sling/bin/`. For Postgres / MySQL / MSSQL / Snowflake / BigQuery targets there's no first-run download — those drivers ship in the Sling binary.
- **Sequencing is real.** The `deps:` chain in the YAML is what enforces dim→fact→view ordering. With DuckDB targets it also avoids concurrent-writer contention (DuckDB is single-writer per file). With Snowflake / BigQuery / Redshift targets, concurrent writes work natively and you can drop the `deps:` if you don't want the ordering.
- **No `pandas` involvement.** Sling streams rows directly from source to target. Memory footprint is constant regardless of table size. This is the key differentiator vs. `sql_to_database_asset` (which lands the source in a DataFrame).
- **Watermark state.** With `mode: incremental`, Sling stores its watermark inside the target schema (e.g. `_sling.runs` table). Re-runs pick up automatically — no need to manage state in env vars or files.

## Companion component for in-memory loads

For DataFrames already materialized in Dagster (e.g. after pandas transforms) and you need them in Snowflake at speed, use `dataframe_to_snowflake_bulk`. Different shape:

| | `database_replication` | `dataframe_to_snowflake_bulk` |
|---|---|---|
| Input | Source SQL DB | Upstream DataFrame asset |
| Engine | Sling (streaming) | Chunked parquet → PUT → COPY INTO |
| Memory | Constant | One chunk at a time (default 500k rows) |
| When to use | Bulk SQL→SQL moves of any size | Mid-size DataFrames after transforms |
| Target | Any of 13 SQL DBs | Snowflake only |

## See also

- [`oracle.md`](oracle.md) — Oracle Free in Docker + connectivity demo (source-side for this pattern)
- [`db2.md`](db2.md) — IBM Db2 in Docker (sibling source-side)
- [`postgres_resource`](https://dagster-component-ui.vercel.app/c/postgres_resource), [`mssql_resource`](https://dagster-component-ui.vercel.app/c/mssql_resource) — same source-side family, OSS backends
- [`dataframe_to_snowflake_bulk`](https://dagster-component-ui.vercel.app/c/dataframe_to_snowflake_bulk) — companion for in-memory bulk loads
- Official `dagster-sling` docs — when you need full Sling YAML control
