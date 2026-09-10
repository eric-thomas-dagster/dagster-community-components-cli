# sql_to_database_asset demo (DuckDB → DuckDB)
> ✅ **Local, hermetic** — no external DB, no auth. Both source + sink are local DuckDB files.

Proves `sql_to_database_asset` end-to-end against two DuckDB files
(source + sink). The component reads via `pandas.read_sql` from the
source SQLAlchemy URL, writes via `pandas.to_sql` to the destination
URL, and emits an `AssetMaterialization`. Same SQLAlchemy contract on
both sides — the same YAML retargets to any dialect.

```
source.duckdb  (5 rows in `contacts`)
  │  SOURCE_DB_URL      (duckdb-engine)
  ▼
sql_to_database_asset  (asset: raw_contacts_sync)
  │  DESTINATION_DB_URL
  ▼
sink.duckdb    (5 rows in `raw_contacts`)
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `sql_to_database_asset` | ingestion | Copy rows from any SQLAlchemy source to any SQLAlchemy destination |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_sql_to_database_asset_demo.sh | bash
cd sql-to-database-asset-demo
export SOURCE_DB_URL="duckdb:///$(pwd)/out/source.duckdb"
export DESTINATION_DB_URL="duckdb:///$(pwd)/out/sink.duckdb"

uv run dg check defs      # YAML validates + defs load
uv run dg launch --assets 'raw_contacts_sync'
```

Expected in the run log:

```
raw_contacts_sync - Reading from source: contacts
raw_contacts_sync - Read 5 rows, 4 columns from source
raw_contacts_sync - Wrote 5 rows to raw_contacts
raw_contacts_sync - ASSET_MATERIALIZATION - Materialized value raw_contacts_sync.
```

## Verify the destination

```bash
uv run python -c "
import duckdb
c = duckdb.connect('$(pwd)/out/sink.duckdb')
print('row count:', c.execute('SELECT COUNT(*) FROM raw_contacts').fetchone()[0])
for r in c.execute('SELECT id, email, company FROM raw_contacts ORDER BY id').fetchall():
    print(' ', r)
"
```

Expected:

```
row count: 5
  (1, 'alice@acme.com', 'Acme')
  (2, 'bob@widgets.com', 'Widgets')
  (3, 'carol@cogs.io', 'Cogs')
  (4, 'dave@sprockets.co', 'Sprockets')
  (5, 'eve@gears.dev', 'Gears')
```

## Full UI flow

```bash
SOURCE_DB_URL="duckdb:///$(pwd)/out/source.duckdb" \
DESTINATION_DB_URL="duckdb:///$(pwd)/out/sink.duckdb" \
uv run dg dev
```

- **Assets** tab → `raw_contacts_sync` — one materialization per run, metadata shows source/dest table names + rows_written.

## Incremental mode

Swap `if_exists: replace` for `if_exists: append` + add a
`watermark_column`/`watermark_env_var` pair to pick up only new rows past
the last-seen watermark:

```yaml
attributes:
  # ...
  if_exists: append
  watermark_column: updated_at
  watermark_env_var: CONTACTS_WATERMARK
```

The component stores the last-seen watermark in the env var referenced
by `watermark_env_var` (persisted between runs via the process env, or
you can shim it through a state-backing resource for durable storage).
The next run's SELECT applies `WHERE updated_at > <cached>`.

## Retargeting to production

Swap either or both `*_DB_URL` env vars for any SQLAlchemy connection
string:

| Source | Destination | Common pattern |
|---|---|---|
| MSSQL | Postgres | Warehouse migration off legacy SQL Server |
| Postgres | Snowflake | Onboarding a new source system |
| MySQL | BigQuery | Analytics landing |
| DuckDB | DuckDB | Local ETL, batched file rebuilds |

No YAML change beyond the env-var values — pandas + SQLAlchemy handle
the dialect specifics.

## See also

- **[sql_monitor](sql_monitor.md)** — sensor sibling that triggers a job per new row (this component does bulk copy on a schedule / on demand).
- **[sql_observation_sensor](sql_observation_sensor.md)** — observation-only sibling (no data copy, just metadata + data_version).
- **[database_replication](https://dagster-component-ui.vercel.app/c/database_replication)** — Sling-backed CDC replication for higher-throughput cases.
- Browse the [walkthrough index](README.md).
