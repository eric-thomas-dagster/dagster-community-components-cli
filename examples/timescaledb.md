# TimescaleDB — DataFrame → hypertable → summary (live, Docker)
> ❌ **Dagster+ Serverless / Hybrid:** local-only demo — requires container/server dependency.

Single-container Docker walkthrough. Spins up `timescale/timescaledb:latest-pg16` locally, scaffolds a Dagster project that generates IoT sensor time-series, lands them into a Postgres table, converts that table to a TimescaleDB hypertable via `create_hypertable(...)`, and returns a per-sensor-type summary.

**Live-validated** — `setup_timescaledb_demo.sh` + `dg launch --assets '*'` materializes 10,000 synthetic sensor samples into a hypertable against a real container. End-to-end run completes in ~6s; verification SQL confirms 1 chunk + 10,000 rows + 6 sensor types with realistic averages (temperature ~23°C, pressure ~1000hPa, humidity ~50%, light ~494 lux, sound ~60dB, motion 0/1).

## Components used

| Component | Role |
|---|---|
| [`synthetic_data_generator`](https://dagster-component-ui.vercel.app/c/synthetic_data_generator) | Generates 10,000 IoT sensor readings (`schema_type: sensors`) — timestamp + value + 5 label columns (sensor_id, sensor_type, location, unit, status) |
| [`timescaledb_resource`](https://dagster-component-ui.vercel.app/c/timescaledb_resource) | Postgres-extension resource with hypertable / compression / retention helpers; included for catalog discoverability |
| [`dataframe_to_table`](https://dagster-component-ui.vercel.app/c/dataframe_to_table) | Loads the DataFrame into TimescaleDB as a regular Postgres table via `DataFrame.to_sql` |
| [`sql_transform`](https://dagster-component-ui.vercel.app/c/sql_transform) | Casts the timestamp column to `timestamptz`, calls `create_hypertable(...)` (idempotent via `if_not_exists`), and returns a per-sensor-type aggregate |

## Architecture

```
sensor_readings (synthetic_data_generator)
      │
      ▼
timescaledb_sensor_load (dataframe_to_table → regular Postgres table)
      │
      ▼
sensor_hypertable (sql_transform → ALTER timestamp + create_hypertable + summary)
```

## Run

```bash
bash setup_timescaledb_demo.sh timescaledb-demo

cd timescaledb-demo
export TIMESCALEDB_URL='postgresql+psycopg2://postgres:postgres-demo@localhost:15432/metrics'

uv run dg launch --assets '*'
# OR uv run dg dev → http://localhost:3000
```

## Verify

```bash
docker exec -i timescaledb-demo-server psql -U postgres -d metrics <<'SQL'
  SELECT hypertable_name, num_chunks
    FROM timescaledb_information.hypertables
   WHERE hypertable_name='sensor_readings';
  SELECT count(*) FROM sensor_readings;
  SELECT sensor_type, count(*), avg(value)::numeric(10,2)
    FROM sensor_readings GROUP BY sensor_type ORDER BY sensor_type;
SQL
```

Expected output:

```
 hypertable_name | num_chunks
-----------------+------------
 sensor_readings |          1

 count
-------
 10000

 sensor_type |  n   | avg_value
-------------+------+-----------
 humidity    | 1663 |     49.82
 light       | 1689 |    494.05
 motion      | 1653 |      0.48
 pressure    | 1679 |   1000.08
 sound       | 1666 |     59.63
 temperature | 1650 |     22.97
```

## Why the ALTER TABLE before create_hypertable

`synthetic_data_generator`'s `sensors` schema emits timestamps as ISO-format strings (`strftime("%Y-%m-%d %H:%M:%S")`). `pandas.to_sql` defaults to landing string columns as `TEXT` on Postgres — but `create_hypertable(...)` requires its time dimension column to be a real `TIMESTAMP` / `TIMESTAMPTZ`. Without the cast, you'll see:

```
psycopg2.errors.WrongObjectType: invalid type for dimension "timestamp"
```

The demo's `sql_transform` runs the cast in place via `ALTER TABLE … ALTER COLUMN "timestamp" TYPE timestamptz USING "timestamp"::timestamptz` before invoking `create_hypertable(...)`. On a 10k-row table this is instant; on larger tables you may want to set the timestamp column type at load time instead — `dataframe_to_table` accepts a `sqlalchemy_types:` field if you want to bypass the pandas-inferred TEXT mapping.

## What the script does

1. **Starts TimescaleDB single-container in Docker** (`timescale/timescaledb:latest-pg16`) on host port 15432.
2. **Polls until CREATE EXTENSION succeeds** — the TimescaleDB image auto-tunes on first launch, which restarts Postgres once after `pg_isready` first reports green. We wait for the extension-create to actually succeed instead of trusting `pg_isready` alone.
3. **Scaffolds the Dagster project** with `uvx create-dagster project`.
4. **Installs the 4 components** via the community CLI (`--refresh` on first call to bust the manifest cache).
5. **Overwrites the CLI-installed example defs.yamls** with demo-specific configuration.

## Teardown

```bash
docker rm -f timescaledb-demo-server
```

## Common issues

- **`invalid type for dimension "timestamp"`** — the timestamp column is `TEXT`, not `TIMESTAMP`. The demo's `sql_transform` ALTERs the column in place before `create_hypertable(...)`; if you're adapting this pattern outside the demo, do the same cast or use `dataframe_to_table`'s `sqlalchemy_types:` to land the column as `TIMESTAMP` directly.
- **`extension "timescaledb" is not available`** — you're on plain Postgres, not the timescale/timescaledb image. The demo pins `timescale/timescaledb:latest-pg16` which has the extension preinstalled.
- **AWS Timestream for InfluxDB** — different product (despite the AWS naming). Use the [`influxdb_resource`](https://dagster-component-ui.vercel.app/c/influxdb_resource) + [`dataframe_to_influxdb`](https://dagster-component-ui.vercel.app/c/dataframe_to_influxdb) sink for that one; pointer in [`vendors/influxdb.md`](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/vendors/influxdb.md).

## See also

- [`vendors/timescaledb.md`](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/vendors/timescaledb.md) — TimescaleDB vendor page (1 dedicated component + generic SQL stack)
- [`vendors/influxdb.md`](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/vendors/influxdb.md) — InfluxDB 2.x/3.x (and AWS Timestream for InfluxDB)
- [`vendors/victoriametrics.md`](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/vendors/victoriametrics.md) — Prometheus-compatible TSDB
- [TimescaleDB docs](https://docs.timescale.com/)
- [`create_hypertable()` reference](https://docs.timescale.com/api/latest/hypertable/create_hypertable/)
