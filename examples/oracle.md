# Oracle Database end-to-end — local Docker, no license, no Instant Client
> ❌ **Dagster+ Serverless / Hybrid:** local-only demo — requires container/server dependency.

Read/write Oracle via the new `oracle_resource` + the generic SQL component family. Same components target the full Oracle estate unchanged — only `host` / `port` / `service_name` change:

- **Oracle Database Free** (`container-registry.oracle.com/database/free`) — this demo
- **Oracle XE / Enterprise** on-prem
- **Oracle Autonomous Database** (Shared / Dedicated) on OCI
- **OCI Base DB Service**

## Components used

| Component | Source | Role |
|---|---|---|
| `oracle_resource` | community (new) | Shared connection — `host` / `port` / `service_name` / auth |
| `synthetic_data_generator` | community | Upstream — 20 synthetic orders |
| `dataframe_to_table` | community | Writes DataFrame → Oracle table via SQLAlchemy (`oracledb` thin-mode) |
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
   │ orders_in_oracle        │  to_sql via oracle+oracledb://
   │ (dataframe_to_table)    │  with NUMBER(38,10) for floats
   └────────────┬────────────┘
                ▼
   ┌─────────────────────────────────────────────┐
   │ Oracle Database Free (Docker, port 1521)    │
   │ service: FREEPDB1                           │
   │ table: SYSTEM.ORDERS — 20 rows              │
   └─────────────────────────────────────────────┘
```

## Run

```bash
bash setup_oracle_demo.sh
cd oracle-demo
source .env.demo

uv run dg check defs
uv run dg launch --assets '*'

# Verify rows in Oracle:
docker exec dg-oracle-demo sh -c \
  "echo 'SELECT COUNT(*) FROM orders;' | sqlplus -S system/DagsterDemo1@//localhost:1521/FREEPDB1"
# COUNT(*) = 20
```

Cleanup: `docker rm -f dg-oracle-demo`.

## YAML shape

```yaml
type: dagster_component_templates.OracleResourceComponent
attributes:
  resource_key: oracle_resource
  host: localhost
  port: 1521
  service_name: FREEPDB1            # OR sid: ORCL
  username: system
  password_env_var: ORACLE_PASSWORD
  # thick_mode: true               # set if you need Oracle Instant Client features
```

```yaml
type: dagster_component_templates.DataframeToTableComponent
attributes:
  asset_name: orders_in_oracle
  upstream_asset_key: synthetic_orders
  database_url_env_var: ORACLE_URL   # oracle+oracledb://user:pass@host:port/?service_name=...
  table_name: orders
  if_exists: replace
```

## Demo notes

- **Oracle Free image is multi-arch** — works on Apple Silicon natively (the older `oracle/database:21.3.0-xe` only had amd64 + needed Rosetta).
- **First boot is slow** — Oracle's container init takes 60-90 seconds. The setup script waits for `DATABASE IS READY TO USE` in the container logs.
- **Float handling** — Oracle's FLOAT type requires explicit `binary_precision`. SQLAlchemy refuses to auto-create FLOAT columns against Oracle. `dataframe_to_table` detects the Oracle dialect and maps pandas float columns to `NUMBER(38,10)`. This fix is in the component — no demo-side hack.
- **Thin mode by default** — `python-oracledb` thin mode is pure Python, no Oracle Instant Client install required. Opt into thick mode (`thick_mode: true`) for features that need it (wallets for ADB with mTLS, advanced OCI calls).

## Production retargeting

```yaml
# Oracle Autonomous Database (Shared)
attributes:
  host: adb.us-ashburn-1.oraclecloud.com
  port: 1522
  service_name: g1234abcd_my_adb_medium.adb.oraclecloud.com
  username: ADMIN
  password_env_var: ADW_PASSWORD
```

For Autonomous DB connections that need a wallet, set `thick_mode: true` and configure `TNS_ADMIN` to the wallet dir.

## See also

- [`db2.md`](db2.md) — sibling proprietary DB family
- [`postgres_resource`](https://dagster-component-ui.vercel.app/c/postgres_resource), [`mssql_resource`](https://dagster-component-ui.vercel.app/c/mssql_resource) — same shape, OSS backends
- `dataframe_to_table`, `sql_command_job`, `warehouse_maintenance_job` — work with any SQLAlchemy URL, Oracle included
