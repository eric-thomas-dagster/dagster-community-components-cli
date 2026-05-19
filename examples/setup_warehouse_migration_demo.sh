#!/usr/bin/env bash
# Warehouse migration demo — DDL-first lift+shift from Postgres → DuckDB.
# Exercises five components: inventory + tables_migration + replication
# (with mode: truncate to preserve DDL) + views_migration + dataframe_to_csv.
#
# REPLICATION vs. MIGRATION
#   This is the BROADER one-time migration story (data + DDL + views).
#   For just the recurring data sync, see replication.md / setup_replication_demo.sh.
#
# Components exercised (5):
#   - database_schema_inventory   (DataFrame of every source object)
#   - database_tables_migration   (CREATE TABLE w/ PK+FK+NOT NULL+DEFAULT) ← NEW
#   - database_replication        (Sling, mode: truncate to preserve DDL)
#   - database_views_migration    (CREATE OR REPLACE VIEW + substitutions) ← NEW
#   - dataframe_to_csv            (the migration completion report)
#
# Seed data (the source DB has FULL DDL so the demo proves it's preserved):
#   - 2 tables (customers + orders), with PKs, FK orders → customers, NOT NULL,
#     DEFAULTs ('pending', CURRENT_TIMESTAMP)
#   - 1 view (v_orders_summary — aggregation; reachable on target)
#   - 2 functions, 1 sequence, 1 trigger (inventoried but NOT migrated —
#     these need LLM-assisted rewrite, see walkthrough)
#
# REQUIRES: Docker daemon running, network access on first run (Sling fetches
#           its DuckDB CLI binary).
# COST: \$0 — Postgres + DuckDB are OSS.

set -euo pipefail
if ! docker info >/dev/null 2>&1; then echo "ERROR: Docker daemon not running."; exit 1; fi

PROJECT_DIR="${1:-warehouse-migration-demo}"
PG_NAME=dg-wm-postgres
PG_PORT=5444
PG_PWD=DagsterDemo1
PG_SRC_DB=legacy_app
DUCKDB_PATH=/tmp/wm-warehouse.duckdb

echo ">>> 1/6  Starting Postgres (legacy DB) in Docker"
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

echo ">>> 2/6  Seeding source schema with FULL DDL (PK + FK + NOT NULL + DEFAULT) and aux objects"
docker exec -i "$PG_NAME" psql -U postgres -d "$PG_SRC_DB" >/dev/null <<'SQL'
CREATE SCHEMA app;

-- Customers (tables migrate automatically with all DDL preserved)
CREATE TABLE app.customers (
  customer_id INT PRIMARY KEY,
  name        TEXT NOT NULL,
  email       TEXT,
  region      TEXT,
  signup_date DATE,
  updated_at  TIMESTAMP DEFAULT NOW()
);
INSERT INTO app.customers
SELECT i, 'Customer '||i, 'c'||i||'@example.com',
       CASE i % 4 WHEN 0 THEN 'US' WHEN 1 THEN 'EU' WHEN 2 THEN 'APAC' ELSE 'LATAM' END,
       DATE '2023-01-01' + (i*7) % 700,
       NOW() - (i*11||' hours')::INTERVAL
FROM generate_series(1, 50) i;

-- Orders (PK + FK to customers + multiple NOT NULL + DEFAULT)
CREATE TABLE app.orders (
  order_id    INT PRIMARY KEY,
  customer_id INT NOT NULL REFERENCES app.customers(customer_id),
  amount      NUMERIC(10,2),
  status      TEXT NOT NULL DEFAULT 'pending',
  region      TEXT,
  created_at  TIMESTAMP,
  updated_at  TIMESTAMP DEFAULT NOW()
);
INSERT INTO app.orders
SELECT i, ((i-1) % 50)+1,
       (random()*500+10)::NUMERIC(10,2),
       CASE i%5 WHEN 0 THEN 'pending' WHEN 1 THEN 'paid' WHEN 2 THEN 'shipped' WHEN 3 THEN 'delivered' ELSE 'cancelled' END,
       CASE i%4 WHEN 0 THEN 'US' WHEN 1 THEN 'EU' WHEN 2 THEN 'APAC' ELSE 'LATAM' END,
       NOW() - (i*7||' minutes')::INTERVAL,
       NOW() - (i*7||' minutes')::INTERVAL
FROM generate_series(1, 200) i;

-- Views (migrate via database_views_migration)
CREATE OR REPLACE VIEW app.v_orders_summary AS
  SELECT region, status, COUNT(*) AS n, AVG(amount) AS avg_amount
  FROM app.orders GROUP BY region, status;

-- Aux objects (inventoried but NOT auto-migrated — these are the LLM-rewrite items)
CREATE OR REPLACE FUNCTION app.fn_customer_count() RETURNS BIGINT AS $$
  SELECT COUNT(*) FROM app.customers;
$$ LANGUAGE SQL;

CREATE OR REPLACE FUNCTION app.tg_touch_updated_at() RETURNS TRIGGER AS $$
  BEGIN NEW.updated_at := NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER tr_orders_touch
  BEFORE UPDATE ON app.orders
  FOR EACH ROW EXECUTE FUNCTION app.tg_touch_updated_at();

CREATE SEQUENCE app.order_seq START 1000;
SQL

echo "    Source DDL summary:"
docker exec "$PG_NAME" psql -U postgres -d "$PG_SRC_DB" -c "
  SELECT 'tables' AS kind, COUNT(*) FROM information_schema.tables WHERE table_schema='app' AND table_type='BASE TABLE'
  UNION ALL SELECT 'views', COUNT(*) FROM information_schema.views WHERE table_schema='app'
  UNION ALL SELECT 'functions', COUNT(*) FROM information_schema.routines WHERE routine_schema='app'
  UNION ALL SELECT 'sequences', COUNT(*) FROM information_schema.sequences WHERE sequence_schema='app'
  UNION ALL SELECT 'triggers', COUNT(*) FROM information_schema.triggers WHERE trigger_schema='app';"

echo ">>> 3/6  Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q 'dagster-sling>=0.24.0' 'sqlalchemy>=2.0.0' psycopg2-binary duckdb-engine tabulate

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> 4/6  Installing 5 components"
$CLI add database_schema_inventory  --auto-install
$CLI add database_tables_migration  --auto-install
$CLI add database_replication       --auto-install
$CLI add database_views_migration   --auto-install
$CLI add dataframe_to_csv           --auto-install

echo ">>> 5/6  Writing defs.yaml (DDL-first flow)"

write_yaml() {
  local name="$1"; shift
  local body="$1"; shift
  mkdir -p "src/$PKG/defs/$name"
  printf "%s\n" "$body" > "src/$PKG/defs/$name/defs.yaml"
}

# 1. Inventory — what's in the source DB?
write_yaml "migration_inventory" "type: $PKG.components.database_schema_inventory.component.DatabaseSchemaInventoryComponent
attributes:
  asset_name: legacy_db_inventory
  connection: postgres://postgres:$PG_PWD@localhost:$PG_PORT/$PG_SRC_DB?sslmode=disable
  database_type: postgres
  schemas: [app]
  group_name: migration_planning"

# 2. Migration plan CSV — pipe the inventory to disk for the team
write_yaml "migration_plan_csv" "type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: migration_plan_csv
  upstream_asset_key: legacy_db_inventory
  file_path: /tmp/legacy_db_migration_plan.csv
  group_name: migration_planning"

# 3. Tables DDL — recreate the schema (types + PK + FK + NOT NULL + DEFAULT) on target
write_yaml "tables_ddl" "type: $PKG.components.database_tables_migration.component.DatabaseTablesMigrationComponent
attributes:
  asset_name: warehouse_ddl_ready
  source_connection: postgres://postgres:$PG_PWD@localhost:$PG_PORT/$PG_SRC_DB?sslmode=disable
  target_connection: duckdb:///$DUCKDB_PATH
  source_type: postgres
  target_type: duckdb
  schemas: [app]
  target_schema: raw
  table_replacements:
    app.customers: raw.customers
    app.orders: raw.orders
  drop_if_exists: true
  deps: [legacy_db_inventory]
  group_name: migration_ddl"

# 4. Data — Sling truncate mode (preserves DDL we just set up)
write_yaml "replicate_customers" "type: $PKG.components.database_replication.component.DatabaseReplicationComponent
attributes:
  asset_name: customers_in_warehouse
  source_connection: postgres://postgres:$PG_PWD@localhost:$PG_PORT/$PG_SRC_DB?sslmode=disable
  target_connection: duckdb:///$DUCKDB_PATH
  source_type: postgres
  source_table: app.customers
  target_type: duckdb
  target_table: raw.customers
  mode: truncate
  deps: [warehouse_ddl_ready]
  group_name: migration_data"

write_yaml "replicate_orders" "type: $PKG.components.database_replication.component.DatabaseReplicationComponent
attributes:
  asset_name: orders_in_warehouse
  source_connection: postgres://postgres:$PG_PWD@localhost:$PG_PORT/$PG_SRC_DB?sslmode=disable
  target_connection: duckdb:///$DUCKDB_PATH
  source_type: postgres
  source_table: app.orders
  target_type: duckdb
  target_table: raw.orders
  mode: truncate
  deps: [customers_in_warehouse]
  group_name: migration_data"

# 5. Views — bulk-migrate, table refs rewritten
write_yaml "views_migration" "type: $PKG.components.database_views_migration.component.DatabaseViewsMigrationComponent
attributes:
  asset_name: views_migrated
  source_connection: postgres://postgres:$PG_PWD@localhost:$PG_PORT/$PG_SRC_DB?sslmode=disable
  target_connection: duckdb:///$DUCKDB_PATH
  source_type: postgres
  target_type: duckdb
  schemas: [app]
  target_schema: raw
  table_replacements:
    app.customers: raw.customers
    app.orders: raw.orders
  deps: [orders_in_warehouse]
  group_name: migration_views"

cat <<MSG

>>> Setup complete.

    cd $PROJECT_DIR
    uv run dg check defs
    uv run dg launch --assets '*'

Verify the migration:
    # 1. DDL preserved on target
    duckdb $DUCKDB_PATH -c "
      DESCRIBE raw.orders;
      SELECT * FROM information_schema.table_constraints WHERE table_schema='raw';"

    # 2. Data loaded
    duckdb $DUCKDB_PATH -c "
      SELECT 'customers' AS tbl, COUNT(*) FROM raw.customers
      UNION ALL SELECT 'orders', COUNT(*) FROM raw.orders
      UNION ALL SELECT 'v_orders_summary', COUNT(*) FROM raw.v_orders_summary;"

    # 3. Migration plan CSV (checklist of objects, including the ones requiring manual LLM-assisted rewrite)
    cat /tmp/legacy_db_migration_plan.csv

Stop + clean up:
    docker rm -f $PG_NAME
    rm -f $DUCKDB_PATH /tmp/legacy_db_migration_plan.csv

Workflow B (data-first) — swap tables_ddl + truncate for:
  - database_replication with mode: full_refresh   (no upfront DDL needed)
  - database_constraints_migration                  (apply PKs/FKs after data lands)
MSG
