#!/usr/bin/env bash
# Recurring SQL→SQL replication demo — Postgres (legacy operational DB)
# → DuckDB (warehouse stand-in) via the dedicated `database_replication`
# component (wraps Sling under the hood).
#
# REPLICATION vs. MIGRATION
#   This demo covers the RECURRING data-sync pattern (run nightly, run
#   incrementally, keep the warehouse in sync). For the one-time
#   warehouse migration story — including PL/SQL procedures, scheduled
#   jobs, views, and other non-table objects — see warehouse_migration.md.
#
# WHY POSTGRES → DUCKDB FOR THE DEMO
#   The exact same component + YAML retargets to Oracle / Db2 / MySQL / MSSQL
#   sources and to Snowflake / BigQuery / Redshift / Databricks targets —
#   only `source_type` / `target_type` and the connection URLs change.
#   Postgres source + DuckDB target gives genuinely different DBs on each
#   end (the real warehouse-migration topology), runs end-to-end in
#   seconds, and needs no cloud credentials.
#
# WHAT THIS DEMONSTRATES
#   1. Full-table replication (mode: full_refresh) — copy `app.customers`
#      from Postgres to DuckDB raw.customers
#   2. Incremental replication (mode: incremental + update_key + primary_key)
#      — `app.orders` flows in, then a delta arrives and only new rows move
#   3. Column subset + WHERE filter — `app.orders` filtered to EU region
#
# Components exercised (1):
#   - database_replication       (3 instances — full_refresh, incremental, filtered)
#
# REQUIRES: Docker daemon running, network access on first run (Sling
#           fetches its bundled DuckDB CLI binary the first time you load
#           a DuckDB target — cached locally afterward).
# COST: \$0 — Postgres and DuckDB are both OSS.

set -euo pipefail
if ! docker info >/dev/null 2>&1; then echo "ERROR: Docker daemon not running."; exit 1; fi

PROJECT_DIR="${1:-replication-demo}"
PG_NAME=dg-replication-postgres
PG_PORT=5444
PG_PWD=DagsterDemo1
PG_SRC_DB=legacy_app
DUCKDB_PATH=/tmp/replication-warehouse.duckdb

echo ">>> 1/6  Starting Postgres (source legacy DB) in Docker"
docker rm -f "$PG_NAME" >/dev/null 2>&1 || true
sleep 1
docker run -d --name "$PG_NAME" \
  -p $PG_PORT:5432 \
  -e POSTGRES_PASSWORD="$PG_PWD" \
  -e POSTGRES_DB="$PG_SRC_DB" \
  postgres:16-alpine >/dev/null

echo "    Waiting for Postgres to accept connections..."
for i in $(seq 1 30); do
  if docker exec "$PG_NAME" pg_isready -U postgres -d "$PG_SRC_DB" >/dev/null 2>&1; then
    echo "    Postgres up after ${i}s."
    break
  fi
  sleep 1
done

echo ">>> 2/6  Seeding source schema: app.customers (50 rows) + app.orders (200 rows)"
docker exec -i "$PG_NAME" psql -U postgres -d "$PG_SRC_DB" >/dev/null <<'SQL'
CREATE SCHEMA app;
CREATE TABLE app.customers (
  customer_id INT PRIMARY KEY,
  name        TEXT NOT NULL,
  email       TEXT,
  region      TEXT,
  signup_date DATE,
  updated_at  TIMESTAMP DEFAULT NOW()
);
INSERT INTO app.customers
SELECT
  i,
  'Customer ' || i,
  'customer' || i || '@example.com',
  CASE i % 4 WHEN 0 THEN 'US' WHEN 1 THEN 'EU' WHEN 2 THEN 'APAC' ELSE 'LATAM' END,
  DATE '2023-01-01' + (i * 7) % 700,
  NOW() - (i * 11 || ' hours')::INTERVAL
FROM generate_series(1, 50) AS i;

CREATE TABLE app.orders (
  order_id    INT PRIMARY KEY,
  customer_id INT NOT NULL,
  amount      NUMERIC(10,2),
  status      TEXT,
  region      TEXT,
  created_at  TIMESTAMP,
  updated_at  TIMESTAMP DEFAULT NOW()
);
INSERT INTO app.orders
SELECT
  i,
  ((i - 1) % 50) + 1,
  (random() * 500 + 10)::NUMERIC(10,2),
  CASE (i % 5) WHEN 0 THEN 'pending' WHEN 1 THEN 'paid' WHEN 2 THEN 'shipped' WHEN 3 THEN 'delivered' ELSE 'cancelled' END,
  CASE i % 4 WHEN 0 THEN 'US' WHEN 1 THEN 'EU' WHEN 2 THEN 'APAC' ELSE 'LATAM' END,
  NOW() - (i * 7 || ' minutes')::INTERVAL,
  NOW() - (i * 7 || ' minutes')::INTERVAL
FROM generate_series(1, 200) AS i;
SQL

echo "    Seeded:"
docker exec "$PG_NAME" psql -U postgres -d "$PG_SRC_DB" -c "SELECT 'customers' AS tbl, COUNT(*) FROM app.customers UNION ALL SELECT 'orders', COUNT(*) FROM app.orders;"

echo ">>> 3/6  Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q 'dagster-sling>=0.24.0'

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> 4/6  Installing components"
$CLI add database_replication --auto-install

echo ">>> 5/6  Writing defs.yaml (3 replication instances)"

write_yaml() {
  local name="$1"; shift
  local body="$1"; shift
  mkdir -p "src/$PKG/defs/$name"
  printf "%s\n" "$body" > "src/$PKG/defs/$name/defs.yaml"
}

# Full-refresh customers: small slowly-changing dim table — replace each run
write_yaml "customers_replication" "type: $PKG.components.database_replication.component.DatabaseReplicationComponent
attributes:
  asset_name: customers_warehouse
  source_connection: postgres://postgres:$PG_PWD@localhost:$PG_PORT/$PG_SRC_DB?sslmode=disable
  source_type: postgres
  source_table: app.customers
  target_connection: duckdb://$DUCKDB_PATH
  target_type: duckdb
  target_table: raw.customers
  mode: full_refresh
  group_name: warehouse_migration"

# Incremental orders: big append-mostly table — incremental by updated_at, PK for upserts.
# deps: customers_warehouse — sequences fact-after-dim AND avoids DuckDB single-writer contention.
write_yaml "orders_incremental_replication" "type: $PKG.components.database_replication.component.DatabaseReplicationComponent
attributes:
  asset_name: orders_warehouse
  source_connection: postgres://postgres:$PG_PWD@localhost:$PG_PORT/$PG_SRC_DB?sslmode=disable
  source_type: postgres
  source_table: app.orders
  target_connection: duckdb://$DUCKDB_PATH
  target_type: duckdb
  target_table: raw.orders
  mode: incremental
  incremental_column: updated_at
  primary_key: [order_id]
  deps: [customers_warehouse]
  group_name: warehouse_migration"

# Filtered EU-only subset: column selection + WHERE filter.
# deps: orders_warehouse — last in the chain.
write_yaml "orders_eu_replication" "type: $PKG.components.database_replication.component.DatabaseReplicationComponent
attributes:
  asset_name: orders_eu_warehouse
  source_connection: postgres://postgres:$PG_PWD@localhost:$PG_PORT/$PG_SRC_DB?sslmode=disable
  source_type: postgres
  source_table: app.orders
  target_connection: duckdb://$DUCKDB_PATH
  target_type: duckdb
  target_table: raw.orders_eu
  mode: full_refresh
  select_columns: [order_id, customer_id, amount, status, created_at]
  where_clause: \"region = 'EU'\"
  deps: [orders_warehouse]
  group_name: warehouse_migration"

cat <<MSG

>>> Setup complete.

    cd $PROJECT_DIR
    uv run dg check defs
    uv run dg launch --assets '*'

Verify rows landed in DuckDB warehouse:
    duckdb $DUCKDB_PATH -c "
      SELECT 'customers' AS tbl, COUNT(*) FROM raw.customers
      UNION ALL SELECT 'orders', COUNT(*) FROM raw.orders
      UNION ALL SELECT 'orders_eu', COUNT(*) FROM raw.orders_eu;"

Try incremental: add 10 new orders, re-run, only the new rows move:
    docker exec $PG_NAME psql -U postgres -d $PG_SRC_DB -c \\
      "INSERT INTO app.orders SELECT 200+i, i, (random()*500+10)::NUMERIC(10,2), 'paid', 'US', NOW(), NOW() FROM generate_series(1,10) i;"
    uv run dg launch --assets orders_warehouse

Stop + clean up:
    docker rm -f $PG_NAME
    rm -f $DUCKDB_PATH

Retargeting (no code changes, just YAML + env):
  • Source → Oracle:     source_type: oracle     + ORACLE://user:pass@host:1521/?service_name=ORCL
  • Source → Db2:        source_type: db2        + DB2://user:pass@host:50000/SAMPLE
  • Source → MSSQL:      source_type: mssql      + SQLSERVER://user:pass@host:1433/db
  • Target → Snowflake:  target_type: snowflake  + SNOWFLAKE://user:pass@account.snowflakecomputing.com/DB/SCHEMA?warehouse=W
  • Target → BigQuery:   target_type: bigquery   + BIGQUERY://project/dataset?keyfile=/path/to/sa.json
  • Target → Redshift:   target_type: redshift   + REDSHIFT://user:pass@cluster.redshift.amazonaws.com:5439/db
  • Target → Databricks: target_type: databricks + DATABRICKS://token:dapi...@host/?http_path=/sql/...
MSG
