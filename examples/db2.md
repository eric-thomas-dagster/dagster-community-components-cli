# IBM Db2 end-to-end — Db2 Community Edition in Docker

Read/write IBM Db2 via the new `db2_resource` + the generic SQL component family. Same components target the full Db2 estate unchanged — only `host` / `port` / `ssl` change:

- **Db2 Community Edition** (`icr.io/db2_community/db2`) — this demo, free non-production
- **Db2 LUW** on-prem (Linux / UNIX / Windows)
- **Db2 on Cloud** (IBM Cloud DBaaS) — port 31xxx, SSL required
- **Db2 Warehouse**

## Components used

| Component | Source | Role |
|---|---|---|
| `db2_resource` | community (new) | Shared connection — `host` / `port` / `database` / auth / SSL |
| `synthetic_data_generator` | community | Upstream — 20 synthetic orders |
| `dataframe_to_table` | community | Writes DataFrame → Db2 table via SQLAlchemy (`ibm_db_sa` dialect) |
| `local_parquet_io_manager` | community | Persists intermediate outputs across subprocesses |

## Architecture

```
   ┌─────────────────────────┐
   │ synthetic_orders        │  20 rows × 10 cols
   │ (generator → DataFrame) │
   └────────────┬────────────┘
                │
                ▼
   ┌─────────────────────────┐
   │ orders_in_db2           │  to_sql via db2+ibm_db://
   │ (dataframe_to_table)    │
   └────────────┬────────────┘
                ▼
   ┌─────────────────────────────────────────────┐
   │ Db2 Community Edition (Docker, port 50000)  │
   │ db: TESTDB                                  │
   │ table: DB2INST1.ORDERS — 20 rows            │
   └─────────────────────────────────────────────┘
```

## Run it

```bash
bash setup_db2_demo.sh
cd db2-demo
source .env.demo

uv run dg check defs
uv run dg launch --assets '*'

# Verify rows in Db2:
docker exec dg-db2-demo su - db2inst1 -c \
  "db2 connect to testdb; db2 'SELECT COUNT(*) FROM db2inst1.ORDERS'"
# COUNT(*) = 20
```

Cleanup: `docker rm -f dg-db2-demo`.

## YAML shape

```yaml
type: dagster_component_templates.Db2ResourceComponent
attributes:
  resource_key: db2_resource
  host: localhost
  port: 50000
  database: testdb
  username: db2inst1
  password_env_var: DB2_PASSWORD
  # ssl: true                    # for Db2 on Cloud
```

```yaml
type: dagster_component_templates.DataframeToTableComponent
attributes:
  asset_name: orders_in_db2
  upstream_asset_key: synthetic_orders
  database_url_env_var: DB2_URL   # db2+ibm_db://user:pass@host:port/database
  table_name: ORDERS
  if_exists: replace
```

## Demo notes

- **amd64-only image.** Db2 Community Edition doesn't publish an arm64 build. On Apple Silicon Macs the setup script passes `--platform linux/amd64` and Docker uses Rosetta — slower than native but works.
- **Privileged container required.** The Db2 product sets kernel parameters at startup; `--privileged` is non-negotiable.
- **License acceptance.** The container needs `LICENSE=accept` passed at run time — this is the IBM Db2 Community Edition license.
- **First boot is slow.** Db2 takes 60-120s to initialize the database. The setup script polls the container logs for `Setup has completed`.
- **`ibm_db` wheel bundles clidriver.** No separate Db2 client install required — the Python wheel ships the necessary native libraries for Linux + macOS + Windows.

## Production retargeting

```yaml
# Db2 on Cloud (IBM Cloud DBaaS)
attributes:
  host: '<id>.databases.appdomain.cloud'
  port: 31198
  database: bludb
  username: bluadmin
  password_env_var: DB2_CLOUD_PASSWORD
  ssl: true
```

For Db2 Warehouse, identical shape — same `ibm_db_sa` driver, different host.

## See also

- [`oracle.md`](oracle.md) — sibling proprietary DB family
- [`postgres_resource`](https://dagster-component-ui.vercel.app/c/postgres_resource), [`mssql_resource`](https://dagster-component-ui.vercel.app/c/mssql_resource) — same shape, OSS backends
- `dataframe_to_table`, `sql_command_job`, `warehouse_maintenance_job` — work with any SQLAlchemy URL, Db2 included
