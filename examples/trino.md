# Trino end-to-end — local docker, no auth

Connect to Trino via the community-component family using a single-container Trino coordinator with built-in `memory` catalog.

## Components used

| Component | Source | Role |
|---|---|---|
| `trino_resource` | community | Shared connection — host / port / catalog / schema / auth |
| `trino_io_manager` | community | Asset IO manager — DataFrame ↔ Trino via partition-aware DELETE+INSERT (**only works against catalogs that support DELETE**) |

## Run it

```bash
bash setup_trino_demo.sh
cd trino-demo
uv run dg check defs
uv run dg list defs    # both components load + resolve

# Validate connectivity end-to-end:
docker exec dg-trino-demo trino --server localhost:8080 --execute 'SHOW CATALOGS;'
# memory, system, tpcds, tpch, jmx
```

Cleanup: `docker rm -f dg-trino-demo`.

## Why end-to-end materialize isn't shown

`trino_io_manager` wraps every write in a transactional **DELETE + INSERT** to support idempotent partition replacement. Trino's `memory` catalog (and `system`/`tpcds`/`tpch`) is **read-only / non-modifying** — it rejects DELETE with:

```
TrinoUserError: NOT_SUPPORTED: This connector does not support modifying table rows
```

To exercise full write semantics, point at a connector that supports DELETE:

- **Iceberg** via Polaris / Nessie / S3 Tables / Snowflake-managed catalog
- **Delta** via the Delta Lake connector
- **Hive** via a real Hive Metastore
- **PostgreSQL** (`postgresql` connector)

For local end-to-end, the cleanest path is **Iceberg with a SQL catalog backed by SQLite** — see `lakehouse_local.md` for the Iceberg-only side of this. A future demo could combine `trino_io_manager` with that catalog.

## YAML shape

```yaml
type: dagster_component_templates.TrinoIOManagerComponent
attributes:
  resource_key: io_manager
  host: localhost
  port: 8080
  user: dagster
  catalog: iceberg           # must support DELETE
  default_schema: warehouse
  password_env_var: TRINO_PW # optional
  partition_column: partition_key
```

```yaml
type: dagster_component_templates.TrinoResourceComponent
attributes:
  resource_key: trino_resource
  host: localhost
  port: 8080
  user: dagster
  catalog: iceberg
  schema_name: warehouse
```

## See also

- [`lakehouse_local.md`](lakehouse_local.md) — Iceberg roundtrip (Trino can read these same tables)
