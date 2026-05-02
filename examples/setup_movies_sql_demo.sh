#!/usr/bin/env bash
# IMDB Top 250 → SQL demo — canonical create-dagster + dg.
#
# Pulls the IMDB Top 250 dataset (CSV from a public mirror, no auth),
# parses the year as an int, computes a decade column, lands the result
# in a local SQLite database via dataframe_to_table.
#
# Pipeline (4 components, all autoloaded by `dg`):
#     csv_file_ingestion → type_coercer → formula → dataframe_to_table

set -euo pipefail

PROJECT_DIR="${1:-movies-sql-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas requests sqlalchemy
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 4 community components into src/$PKG/components/ + defs/"
$CLI add csv_file_ingestion    --auto-install
$CLI add type_coercer          --auto-install
$CLI add formula               --auto-install
$CLI add dataframe_to_table    --auto-install

echo ">>> Writing demo defs.yaml for each component"

# 1. Ingest — IMDB Top 250 CSV (mirror that's stable + public; subject to host availability)
cat > "src/$PKG/defs/csv_file_ingestion/defs.yaml" <<EOF
type: $PKG.components.csv_file_ingestion.component.CSVFileIngestionComponent
attributes:
  asset_name: movies_raw
  file_path: https://raw.githubusercontent.com/erajasekar/IMDB-Top-250-Movies-Dataset/refs/heads/master/IMDB%20Top%20250%20Movies.csv
  description: IMDB Top 250 Movies (public CSV mirror)
  group_name: ingest
EOF

# 2. Coerce types — `year` came in as a string; we want it as int for math
cat > "src/$PKG/defs/type_coercer/defs.yaml" <<EOF
type: $PKG.components.type_coercer.component.TypeCoercerComponent
attributes:
  asset_name: movies_typed
  upstream_asset_key: movies_raw
  type_mapping:
    year: int
    rating: float
  group_name: transform
EOF

# 3. Compute a decade column — formula evaluates pandas expressions
cat > "src/$PKG/defs/formula/defs.yaml" <<EOF
type: $PKG.components.formula.component.FormulaComponent
attributes:
  asset_name: movies_with_decade
  upstream_asset_key: movies_typed
  expressions:
    decade: "(year // 10) * 10"
  group_name: transform
EOF

# 4. Write to SQLite — no server needed, just a file. The DATABASE_URL env
# var is what dataframe_to_table reads; SQLAlchemy URL points at /tmp.
cat > "src/$PKG/defs/dataframe_to_table/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_table.component.DataframeToTableComponent
attributes:
  asset_name: movies_table
  upstream_asset_key: movies_with_decade
  table_name: top_movies
  database_url_env_var: DATABASE_URL
  if_exists: replace
  drop_timezone: true
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Materialize headlessly (point DATABASE_URL at a SQLite file):
    cd $PROJECT_DIR
    DATABASE_URL=sqlite:////tmp/movies.db uv run dg launch --assets '*'

Or open the UI (set the env var the same way):
    DATABASE_URL=sqlite:////tmp/movies.db uv run dg dev

Inspect the result:
    sqlite3 /tmp/movies.db <<SQL
      .headers on
      .mode column
      SELECT decade, COUNT(*) AS movies, ROUND(AVG(rating), 2) AS avg_rating
      FROM top_movies GROUP BY decade ORDER BY decade DESC;
    SQL

You'll see the IMDB Top 250 grouped by release decade, with average rating
per decade. Same query works against any backend — flip DATABASE_URL to
postgresql://… or mysql://… and the same pipeline lands data there instead.
MSG
