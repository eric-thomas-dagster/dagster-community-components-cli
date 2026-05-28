#!/usr/bin/env bash
# TimescaleDB end-to-end demo — single-container Docker.
#
# WHAT THIS DEMONSTRATES
#   The TimescaleDB community component shipped in v0.10.1:
#
#     1. `timescaledb_resource`  Postgres-extension resource with hypertable /
#                                compression / retention helpers
#
#   Plus the generic SQL stack (which works against TimescaleDB because it's
#   Postgres on the wire):
#
#     2. `synthetic_data_generator`  IoT sensor time-series → DataFrame
#     3. `dataframe_to_table`        Pandas → TimescaleDB (creates a regular table)
#     4. `sql_transform`             converts the regular table → hypertable via
#                                    SELECT create_hypertable(...)
#
# Demonstrates the right pattern: load → convert to hypertable → query.
#
# COST: $0 — TimescaleDB in Docker (Postgres + TimescaleDB extension preinstalled).

set -euo pipefail
PROJECT_DIR="${1:-timescaledb-demo}"
TS_PORT="${TS_PORT:-15432}"
TS_CONTAINER="${TS_CONTAINER:-timescaledb-demo-server}"
TS_USER="${TS_USER:-postgres}"
TS_PASSWORD="${TS_PASSWORD:-postgres-demo}"
TS_DB="${TS_DB:-metrics}"

# --- 1. Bring up TimescaleDB ---
echo ">>> Starting TimescaleDB in Docker (container: $TS_CONTAINER)"
docker rm -f "$TS_CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$TS_CONTAINER" \
  -p "$TS_PORT:5432" \
  -e POSTGRES_USER="$TS_USER" \
  -e POSTGRES_PASSWORD="$TS_PASSWORD" \
  -e POSTGRES_DB="$TS_DB" \
  timescale/timescaledb:latest-pg16 >/dev/null

echo ">>> Waiting for TimescaleDB to come up..."
# TimescaleDB's official image auto-tunes on first launch, which means it
# restarts once after pg_isready first reports green. We wait for the
# extension-CREATE to actually succeed instead of trusting pg_isready alone.
for i in $(seq 1 60); do
  if docker exec "$TS_CONTAINER" psql -U "$TS_USER" -d "$TS_DB" \
       -c "CREATE EXTENSION IF NOT EXISTS timescaledb" >/dev/null 2>&1; then
    echo "    ready after ${i}s"
    break
  fi
  sleep 1
done

# --- 2. Scaffold the Dagster project ---
echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q 'yarl<1.24' pandas sqlalchemy psycopg2-binary
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing components (--refresh on first call busts the registry-manifest cache)"
$CLI --refresh add synthetic_data_generator --auto-install
$CLI add timescaledb_resource --auto-install
$CLI add dataframe_to_table --auto-install
$CLI add sql_transform --auto-install

echo ">>> Overwriting CLI-installed example defs.yamls with demo-specific config"

# synthetic_data_generator → sensor readings.
cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: sensor_readings
  schema_type: sensors
  row_count: 10000
  group_name: source
EOF

# dataframe_to_table → load DataFrame into Postgres as a regular table.
cat > "src/$PKG/defs/dataframe_to_table/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_table.component.DataframeToTableComponent
attributes:
  asset_name: timescaledb_sensor_load
  upstream_asset_key: sensor_readings
  table: sensor_readings
  if_exists: replace
  database_url_env_var: TIMESCALEDB_URL
  group_name: timescaledb
EOF

# sql_transform → convert the regular table into a TimescaleDB hypertable.
# Uses create_hypertable(...) idempotently. Then summarize by sensor_type.
cat > "src/$PKG/defs/sql_transform/defs.yaml" <<EOF
type: $PKG.components.sql_transform.component.SqlTransformComponent
attributes:
  asset_name: sensor_hypertable
  upstream_asset_keys: [timescaledb_sensor_load]
  connection_url_env_var: TIMESCALEDB_URL
  sql: |
    -- synthetic_data_generator emits timestamps as strings, which
    -- pandas.to_sql lands as TEXT. Hypertables need a real TIMESTAMP
    -- column on the time dimension, so cast in place before converting.
    ALTER TABLE sensor_readings
      ALTER COLUMN "timestamp" TYPE timestamptz
      USING "timestamp"::timestamptz;
    -- Convert the regular table into a TimescaleDB hypertable
    -- (idempotent via if_not_exists). Then return a small summary
    -- DataFrame so the asset has a value.
    SELECT create_hypertable(
      'sensor_readings',
      'timestamp',
      if_not_exists => TRUE,
      migrate_data => TRUE
    );
    SELECT sensor_type, count(*) AS n, avg(value)::numeric(10,2) AS avg_value
      FROM sensor_readings
     GROUP BY sensor_type
     ORDER BY sensor_type;
  return_dataframe: true
  group_name: timescaledb
EOF

# timescaledb_resource — connection resource (catalog discoverability).
cat > "src/$PKG/defs/timescaledb_resource/defs.yaml" <<EOF
type: $PKG.components.timescaledb_resource.component.TimescaleDBResourceComponent
attributes:
  resource_key: timescaledb_resource
  host: localhost
  port: $TS_PORT
  database: $TS_DB
  username: $TS_USER
  password: $TS_PASSWORD
EOF

# --- 3. Final instructions ---
echo ""
echo "============================================================"
echo "TimescaleDB demo scaffolded at $PROJECT_DIR"
echo "============================================================"
echo ""
echo "TimescaleDB (Postgres + extension):"
echo "  Host:     localhost"
echo "  Port:     $TS_PORT"
echo "  Database: $TS_DB"
echo "  User:     $TS_USER  /  Password: $TS_PASSWORD"
echo ""
echo "Next steps:"
echo ""
echo "  cd $PROJECT_DIR"
echo "  export TIMESCALEDB_URL='postgresql+psycopg2://$TS_USER:$TS_PASSWORD@localhost:$TS_PORT/$TS_DB'"
echo ""
echo "  uv run dg launch --assets '*'             # headless materialize"
echo "  # OR uv run dg dev                        # UI at http://localhost:3000"
echo ""
echo "Then verify the table is a hypertable + has rows:"
echo "  docker exec -i $TS_CONTAINER psql -U $TS_USER -d $TS_DB <<'SQL'"
echo "    SELECT * FROM timescaledb_information.hypertables WHERE hypertable_name='sensor_readings';"
echo "    SELECT count(*) FROM sensor_readings;"
echo "    SELECT sensor_type, count(*), avg(value)::numeric(10,2)"
echo "      FROM sensor_readings GROUP BY sensor_type ORDER BY sensor_type;"
echo "  SQL"
echo ""
echo "Cleanup:"
echo "  docker rm -f $TS_CONTAINER"
echo ""
