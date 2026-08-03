# MariaDB — using the MySQL components unchanged

MariaDB is wire-compatible with MySQL: same protocol, same drivers, same tools. This walkthrough validates the existing `MySQLResourceComponent` + `DataframeToTableComponent` against a `mariadb:11` Docker container with **zero component changes** — the same YAML that works against MySQL 8 works against MariaDB 11.

**Setup:**

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_mariadb_demo.sh \
  -o setup_mariadb_demo.sh
bash setup_mariadb_demo.sh
```

Requirements: [uv](https://docs.astral.sh/uv/) + Docker. Cost: $0.

## What gets validated

| Component | Role |
|---|---|
| `MySQLResourceComponent` | Shared connection (host / port / auth) — works against MariaDB via wire compat |
| `SyntheticDataGeneratorComponent` | Upstream DataFrame producer (100 synthetic orders) |
| `DataframeToTableComponent` | Bulk `INSERT` via SQLAlchemy + `pymysql` driver |

## The chain

```
mariadb:11 container  (dagster_mariadb_demo, host-port 13306 → 3306)
   └─ analytics.orders          ← 5 seed rows (setup script)
   └─ analytics.orders_snapshot ← target of the sink asset

┌──────────────────────────┐
│ synthetic_orders         │
│ (SyntheticDataGenerator, │
│  schema_type=orders,     │
│  row_count=100)          │
│ → DataFrame              │
└──────────────┬───────────┘
               │
               ▼
┌──────────────────────────┐
│ orders_snapshot          │
│ (dataframe_to_table sink │
│  via SQLAlchemy +        │
│  pymysql — writes to     │
│  analytics.orders_snapshot│
└──────────────────────────┘
```

## Why MariaDB works with the MySQL components

- `mysql-connector-python` speaks the MySQL wire protocol → MariaDB is a drop-in target.
- SQLAlchemy's `mysql+pymysql` dialect works against both.
- All SQL that `dataframe_to_table` emits (`CREATE TABLE`, `INSERT`, `REPLACE INTO`) is standard MySQL DDL/DML that MariaDB accepts unchanged.

The one thing that differs: **auth setup in the Docker image**. MariaDB 11 defaults to `unix_socket` auth for `root` at the container level, and the client's default TLS negotiation can fail against the emphemeral SSL cert. The setup script sidesteps both by using `--protocol=tcp --skip-ssl` and the app-level `dagster` user (not `root`).

## Docker-image note

`mariadb:11` (official image). ~150 MB. First-boot is 3-5 seconds. The setup script polls with `SELECT 1` on the app user until it succeeds (up to 60s).

Port binding is `13306` → `3306` (default MariaDB port). Override with `MARIADB_HOST_PORT=…` if `13306` is taken.

Env vars the setup script uses:
```
MARIADB_ROOT_PASSWORD=demo         # root password
MARIADB_DATABASE=analytics         # auto-created database
MARIADB_USER=dagster               # app-level user (used by Dagster + seed script)
MARIADB_PASSWORD=demo              # app-level password
```

## Verifying end-to-end

```bash
docker exec dagster_mariadb_demo mariadb --protocol=tcp -h127.0.0.1 -udagster -pdemo \
  --skip-ssl analytics -e "SELECT COUNT(*) FROM orders_snapshot;"
# → 100
```

## Retargeting at production MariaDB

- **SkySQL / MariaDB Enterprise Server**: same YAML, swap `host` + set `ssl_disabled: false` on the resource. Provide the CA cert path via the driver if strict cert verification is on.
- **Amazon RDS for MariaDB**: same YAML, use the RDS instance endpoint as `host`.
- **Galera Cluster**: point `host` at the HAProxy/MaxScale endpoint that fronts the cluster; app-level ops don't need cluster-awareness.

## Teardown

```bash
docker rm -f dagster_mariadb_demo
```

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_mariadb_demo.sh \
  -o setup_mariadb_demo.sh
bash setup_mariadb_demo.sh
```

## See also

- MySQL 8 walkthrough (same components) — search `dagster-component search mysql`
- [`mysql_resource` component](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/resources/mysql_resource) — schema + registration
- Doris/StarRocks + MySQL protocol — see [`doris_starrocks.md`](doris_starrocks.md) for another wire-compatible target
