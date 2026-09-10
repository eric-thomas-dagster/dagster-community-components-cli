# sql_monitor demo (DuckDB)
> ✅ **Local, hermetic** — no external DB, no auth. DuckDB via SQLAlchemy.

Proves `sql_monitor` end-to-end against a seeded DuckDB file. Sensor
polls a table for rows past its watermark cursor, emits **one
`RunRequest` per new row** with the row's payload threaded into op
config, and advances its cursor so the next tick only sees rows past
that watermark.

```
DuckDB file (out/events.duckdb — table sales_events with id + updated_at)
  │
  │  SQLAlchemy connection (duckdb-engine)
  ▼
sql_monitor  (sensor: SELECT * FROM sales_events WHERE updated_at > <cursor>)
  │
  │  one RunRequest per row (run_key = "sales_events-id-<id>")
  ▼
python_callable_job (fires the callable with row payload as op config)
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `sql_monitor` | sensor | Polls SQL table via SQLAlchemy; emits `RunRequest`s past the watermark |
| 2 | `python_callable_job` | jobs | The target job — invokes `handle(context)` per detected row |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_sql_monitor_demo.sh | bash
cd sql-monitor-demo
export DUCKDB_URL="duckdb:///$(pwd)/out/events.duckdb"

uv run dg check defs   # both YAMLs validate + defs load
```

## First tick — watermark initialization

`sql_monitor` deliberately does NOT emit RunRequests for pre-existing
rows on its first tick. It initializes the watermark cursor to the
current `MAX(updated_at)` and returns 0 RunRequests. This prevents
double-firing historical events after the sensor is deployed.

```bash
uv run python -c "
import os; os.environ['DUCKDB_URL']='$DUCKDB_URL'
from dagster import build_sensor_context, DagsterInstance
from sql_monitor_demo.definitions import defs
sensor = (defs() if callable(defs) else defs).get_sensor_def('sales_events_monitor')
result = sensor(build_sensor_context(instance=DagsterInstance.ephemeral()))
print(f'RunRequests: {len(result.run_requests)}')   # → 0
print(f'cursor:      {result.cursor}')              # → max updated_at from seed
"
```

Expected: `RunRequests: 0` and cursor set to `2026-09-10 09:12:00` (the max seeded `updated_at`).

## Second tick — new row triggers a RunRequest

Insert a row past the watermark + re-fire with the cursor from the previous run:

```bash
uv run python -c "
import duckdb
duckdb.connect('$(pwd)/out/events.duckdb').execute(
    \"INSERT INTO sales_events VALUES (99, 'NewCo', 999.00, '2026-09-10 12:00:00')\"
)
"

uv run python -c "
import os; os.environ['DUCKDB_URL']='$DUCKDB_URL'
from dagster import build_sensor_context, DagsterInstance
from sql_monitor_demo.definitions import defs
sensor = (defs() if callable(defs) else defs).get_sensor_def('sales_events_monitor')
ctx = build_sensor_context(instance=DagsterInstance.ephemeral(), cursor='2026-09-10 09:12:00')
result = sensor(ctx)
print(f'RunRequests: {len(result.run_requests)}')   # → 1
for rr in result.run_requests:
    print(f'  run_key: {rr.run_key}')
    for op_name, cfg in (rr.run_config.get(\"ops\") or {}).items():
        print(f'  op={op_name}  cfg={cfg[\"config\"]}')
print(f'cursor: {result.cursor}')
"
```

Expected:

```
RunRequests: 1
  run_key: sales_events-id-99
  op=config  cfg={'table_name': 'sales_events', 'watermark_column': 'updated_at',
                  'watermark_value': '2026-09-10 12:00:00',
                  'row_id_column': 'id', 'row_id': '99',
                  'row': '{"id": "99", "customer": "NewCo", "amount_usd": "999.0",
                          "updated_at": "2026-09-10 12:00:00"}',
                  ...}
cursor: 2026-09-10 12:00:00
```

The `run_key` is deterministic per row (dedup: if the sensor tick
happens to see the same row twice, Dagster's run-key uniqueness
prevents double-execution). The full row payload is JSON-encoded into
op config so the job's callable receives it via
`context.op_config['row']`.

## Full UI flow

```bash
DUCKDB_URL="duckdb:///$(pwd)/out/events.duckdb" uv run dg dev
```

- **Sensors** tab → `sales_events_monitor` (default-stopped in the demo; toggle on to activate 30s polling).
- **Runs** tab → each detected row produces one run of `process_sales_event` with the row payload visible in the run config.

## Retargeting to production

Swap `DUCKDB_URL` for any SQLAlchemy connection string — Postgres
(`postgresql+psycopg2://...`), MySQL (`mysql+pymysql://...`), Snowflake
(`snowflake://...` via `snowflake-sqlalchemy`), BigQuery (via
`sqlalchemy-bigquery`), Redshift, SQL Server. No component-side
changes needed.

## See also

- **[sql_observation_sensor](sql_observation_sensor.md)** — sibling that observes counts + emits a data_version tag instead of triggering a job per row.
- **[python_callable_job](https://dagster-component-ui.vercel.app/c/python_callable_job)** — target-job component used here.
- Browse the [walkthrough index](README.md).
