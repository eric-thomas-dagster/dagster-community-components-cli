# sql_observation_sensor demo (DuckDB)
> ✅ **Local, hermetic** — no external DB, no auth. DuckDB via SQLAlchemy.

Proves `sql_observation_sensor`'s native SQLAlchemy path end-to-end against a
seeded DuckDB file. The sensor connects, runs `SELECT COUNT(*)` +
`SELECT MAX(watermark_column)`, and emits `AssetMaterialization` on the
external asset with a `dagster/data_version` tag derived from the latest
watermark. Insert a row → next tick's data_version advances → downstream
`AutomationCondition.eager()` fires.

```
DuckDB file (out/orders.duckdb, seeded with sales_orders table)
  │
  │  SQLAlchemy connection (duckdb-engine)
  ▼
external_sql_asset  (declare-only Dagster asset — orders/sales_orders)
  ▲
  │  emits AssetMaterialization every check_interval_seconds
  │
sql_observation_sensor
  ├── row_count metadata     (from SELECT COUNT(*))
  ├── latest_watermark       (from SELECT MAX(updated_at))
  └── dagster/data_version   (tags the event; advances iff watermark moves)
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `external_sql_asset` | external | Declare-only Dagster asset representing the DuckDB `sales_orders` table |
| 2 | `sql_observation_sensor` | observation | Polls the table via SQLAlchemy and emits materialization events with a data-version tag |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_sql_observation_sensor_demo.sh | bash
cd sql-observation-sensor-demo
export DUCKDB_URL="duckdb:///$(pwd)/out/orders.duckdb"

# Validate the schema:
uv run dg check defs

# Fire the sensor once and inspect the emitted event (row_count + data_version):
uv run python -c "
import os; os.environ['DUCKDB_URL']='$DUCKDB_URL'
from dagster import build_sensor_context, DagsterInstance
from sql_observation_sensor_demo.definitions import defs
sensor = (defs() if callable(defs) else defs).get_sensor_def('sales_orders_health')
result = sensor(build_sensor_context(instance=DagsterInstance.ephemeral()))
for e in result.asset_events:
    md = {k: getattr(v, 'value', v) for k, v in dict(e.metadata).items()}
    print('event    :', type(e).__name__)
    print('asset_key:', e.asset_key)
    print('metadata :', md)
    print('tags     :', dict(e.tags or {}))
"
```

Expected output on first tick (3 seeded rows):

```
event    : AssetMaterialization
asset_key: AssetKey(['orders', 'sales_orders'])
metadata : {'row_count': 3, 'table_name': 'sales_orders', 'latest_watermark': '2026-09-10 09:12:00'}
tags     : {'dagster/data_version': '2026-09-10 09:12:00'}
```

## Proving the data_version advances

Insert one row + re-fire the sensor:

```bash
uv run python -c "
import duckdb
duckdb.connect('$(pwd)/out/orders.duckdb').execute(
    \"INSERT INTO sales_orders VALUES (99, 'NewCo', 999.00, '2026-09-10 12:34:56')\"
)
"

# Re-run the sensor snippet above — row_count now 4, data_version now the newer watermark.
```

Result: `row_count=4  data_version=2026-09-10 12:34:56`. Downstream assets keyed
with `AutomationCondition.eager()` on `orders/sales_orders` fire on the next
resolve because the data_version moved.

## Full UI flow

```bash
DUCKDB_URL="duckdb:///$(pwd)/out/orders.duckdb" uv run dg dev
```

- **Assets** tab → `orders/sales_orders` — external asset, tile goes green on each sensor tick that emits materialization.
- **Sensors** tab → `sales_orders_health` — toggle on; polls every 60s in the demo.
- **Runs** tab → each sensor evaluation logs the event; click through to see the `row_count`/`latest_watermark` metadata rendered inline.

## Why this demo matters

`sql_observation_sensor` supports every SQLAlchemy-dialected database
(Postgres, MySQL, SQL Server, Snowflake via `snowflake-sqlalchemy`,
BigQuery via `sqlalchemy-bigquery`, Redshift, ClickHouse, DuckDB, …).
DuckDB is the ideal target for a hermetic demo — same SQLAlchemy contract,
zero infra. Any team can `curl | bash` the setup, run the sensor, verify
the shape, and swap `DUCKDB_URL` for their production connection string
with no other changes.

## See also

- **[external_sql_asset](https://dagster-component-ui.vercel.app/c/external_sql_asset)** — the declare-only asset shape the sensor observes.
- **[dataframe_from_sql](https://dagster-component-ui.vercel.app/c/dataframe_from_sql)** — sibling component for actually reading rows into a DataFrame (as opposed to just observing counts).
- Browse the [walkthrough index](README.md) for other observation-sensor demos across the registry.
