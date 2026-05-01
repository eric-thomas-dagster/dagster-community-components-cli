#!/usr/bin/env bash
# Vintage Cars → SQL demo — canonical create-dagster + dg.
#
# Pulls the classic vega cars dataset (406 cars, 1970-1982, US/Europe/Japan),
# parses the Year string as a real date, derives a `decade` column, and lands
# the result in a local SQLite database via dataframe_to_table — no external
# DB needed, no auth, just a file.
#
# Pipeline (4 components, all autoloaded by `dg`):
#     rest_api_fetcher → datetime_parser → formula → dataframe_to_table

set -euo pipefail

PROJECT_DIR="${1:-cars-sql-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas requests sqlalchemy
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 4 community components into src/$PKG/defs/"
$CLI add rest_api_fetcher      --auto-install
$CLI add datetime_parser       --auto-install
$CLI add formula               --auto-install
$CLI add dataframe_to_table    --auto-install

echo ">>> Writing demo defs.yaml for each component"

# 1. Ingest — vega's classic cars.json (it's a list of records, no json_path needed)
cat > "src/$PKG/defs/rest_api_fetcher/defs.yaml" <<EOF
type: $PKG.defs.rest_api_fetcher.component.RestApiFetcherComponent
attributes:
  asset_name: cars_raw
  api_url: https://raw.githubusercontent.com/vega/vega-datasets/main/data/cars.json
  method: GET
  auth_type: none
  output_format: dataframe
  description: Vega cars dataset — 406 cars, 1970-1982, public no-auth mirror
  group_name: ingest
EOF

# 2. Parse Year — comes in as "YYYY-01-01" strings
cat > "src/$PKG/defs/datetime_parser/defs.yaml" <<EOF
type: $PKG.defs.datetime_parser.component.DatetimeParser
attributes:
  asset_name: cars_typed
  upstream_asset_key: cars_raw
  date_column: Year
  output_column: model_year
  group_name: transform
EOF

# 3. Compute decade column — formula uses pandas dt accessor
cat > "src/$PKG/defs/formula/defs.yaml" <<EOF
type: $PKG.defs.formula.component.FormulaComponent
attributes:
  asset_name: cars_with_decade
  upstream_asset_key: cars_typed
  expressions:
    decade: "(model_year.dt.year // 10) * 10"
  group_name: transform
EOF

# 4. Write to SQLite — DATABASE_URL is read at runtime; drop_timezone=true
# strips tz from model_year (SQLite has no native tz support).
cat > "src/$PKG/defs/dataframe_to_table/defs.yaml" <<EOF
type: $PKG.defs.dataframe_to_table.component.DataframeToTableComponent
attributes:
  asset_name: cars_table
  upstream_asset_key: cars_with_decade
  table_name: cars
  database_url_env_var: DATABASE_URL
  if_exists: replace
  drop_timezone: true
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Materialize headlessly (DATABASE_URL points at any SQLAlchemy-supported DB):
    cd $PROJECT_DIR
    DATABASE_URL=sqlite:////tmp/cars.db uv run dg launch --assets '*'

Or open the UI:
    DATABASE_URL=sqlite:////tmp/cars.db uv run dg dev   # http://localhost:3000

Inspect the result with sqlite3:
    sqlite3 -header -column /tmp/cars.db <<SQL
      SELECT decade, Origin,
             COUNT(*)               AS n_cars,
             ROUND(AVG(Miles_per_Gallon), 1) AS avg_mpg,
             ROUND(AVG(Horsepower), 0)        AS avg_hp
      FROM cars
      GROUP BY decade, Origin
      ORDER BY decade DESC, n_cars DESC;
    SQL

You'll see how MPG climbed and HP fell across the 70s decade, broken down
by US / Europe / Japan. Same pipeline, same defs.yaml — flip DATABASE_URL
to postgresql://… or mysql://… and the data lands there instead.
MSG
