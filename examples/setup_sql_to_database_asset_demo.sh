#!/usr/bin/env bash
# sql_to_database_asset demo — DuckDB source → DuckDB sink.
#
# The component takes a source SQLAlchemy URL + a destination
# SQLAlchemy URL and pipes rows via pandas.read_sql / to_sql. DuckDB
# on both sides is the ideal hermetic proof: same SQLAlchemy contract
# (duckdb-engine), no server, no auth, real network of tables.
#
# Shape (1 component):
#   sql_to_database_asset  source: source.duckdb::contacts
#                          dest:   sink.duckdb::raw_contacts

set -euo pipefail
PROJECT_DIR="${1:-sql-to-database-asset-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"
mkdir -p out
PKG="$(ls src/ | head -1)"

echo ">>> Adding deps"
uv add -q 'sqlalchemy>=2.0.0' 'duckdb>=1.0.0' 'duckdb-engine>=0.13.0' 'pandas'
uv add -q 'yarl<1.24'   # workaround
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --refresh --from dagster-community-components-cli dagster-component --refresh"

echo ">>> Installing 1 community component"
$CLI add sql_to_database_asset  --auto-install

# ── Seed a source DuckDB with a small contacts table.
SOURCE_PATH="$PROJECT_ABS/out/source.duckdb"
SINK_PATH="$PROJECT_ABS/out/sink.duckdb"
SOURCE_URL="duckdb:///$SOURCE_PATH"
SINK_URL="duckdb:///$SINK_PATH"
echo ">>> Seeding source: $SOURCE_PATH"
uv run python - <<PY
import duckdb
c = duckdb.connect("$SOURCE_PATH")
c.execute("DROP TABLE IF EXISTS contacts")
c.execute("""
CREATE TABLE contacts (
    id           INTEGER PRIMARY KEY,
    email        VARCHAR,
    company      VARCHAR,
    updated_at   TIMESTAMP
);
""")
c.execute("""
INSERT INTO contacts VALUES
    (1, 'alice@acme.com',    'Acme',    '2026-09-10 09:00:00'),
    (2, 'bob@widgets.com',   'Widgets', '2026-09-10 09:05:00'),
    (3, 'carol@cogs.io',     'Cogs',    '2026-09-10 09:12:00'),
    (4, 'dave@sprockets.co', 'Sprockets','2026-09-10 09:20:00'),
    (5, 'eve@gears.dev',     'Gears',   '2026-09-10 09:30:00');
""")
print(f"    seeded {c.execute('SELECT COUNT(*) FROM contacts').fetchone()[0]} contacts")
PY

echo ">>> Writing defs.yaml"

cat > "src/$PKG/defs/sql_to_database_asset/defs.yaml" <<EOF
type: $PKG.components.sql_to_database_asset.component.SQLToDatabaseAssetComponent
attributes:
  asset_name: raw_contacts_sync
  source_url_env_var:      SOURCE_DB_URL
  destination_url_env_var: DESTINATION_DB_URL
  source_table:            contacts
  destination_table:       raw_contacts
  if_exists:               replace   # full-refresh each run for the demo
  chunksize:               1000
  group_name:              ingestion
  description:             Sync DuckDB source.contacts → DuckDB sink.raw_contacts
EOF

cat <<MSG

>>> Setup complete.

Export both connection strings:

    export SOURCE_DB_URL='$SOURCE_URL'
    export DESTINATION_DB_URL='$SINK_URL'

Validate:

    cd $PROJECT_DIR && uv run dg check defs

Materialize the sync (one-shot copy):

    cd $PROJECT_DIR && SOURCE_DB_URL='$SOURCE_URL' DESTINATION_DB_URL='$SINK_URL' \\
        uv run dg launch --assets 'raw_contacts_sync'

Verify the destination:

    uv run python -c "
import duckdb
c = duckdb.connect('$SINK_PATH')
print(c.execute('SELECT COUNT(*) FROM raw_contacts').fetchone())
for r in c.execute('SELECT * FROM raw_contacts ORDER BY id').fetchall():
    print(r)
"

Or start the UI:

    cd $PROJECT_DIR && SOURCE_DB_URL='$SOURCE_URL' DESTINATION_DB_URL='$SINK_URL' \\
        uv run dg dev
MSG
