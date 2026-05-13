#!/usr/bin/env bash
# MovieLens Top-250 → SQL demo — 100% components.
#
# Real MovieLens (latest-small) data → Bayesian-smoothed Top-250 → SQLite.
# No inline Python. The whole pipeline is declared in YAML and materialized
# by `dg launch --assets '*'`.
#
# Pipeline (11 components, all autoloaded by `dg`):
#
#   archive_fetcher (ml-latest-small.zip)
#       │
#       ├──> csv_file_ingestion (movies.csv)  ──────────────────────┐
#       │                                                            │
#       └──> csv_file_ingestion (ratings.csv)                        │
#                   │                                                │
#                   ├──> summarize  (avg_rating + num_ratings per movie)
#                   │       │
#                   │       └──> formula  (bayesian-smoothed score)
#                   │               │
#                   │               └──> filter  (num_ratings >= 50)
#                   │                       │
#                   │                       └──> dataframe_join  (movies + ratings_qualifying)
#                   │                               │                ◄──┘
#                   │                               └──> sort  (by bayes desc, limit 250)
#                   │                                       │
#                   │                                       └──> regex_parser  (year)
#                   │                                               │
#                   │                                               └──> formula  (title_clean, decade, rank)
#                   │                                                       │
#                   │                                                       └──> dataframe_to_table  (SQLite)

set -euo pipefail

PROJECT_DIR="${1:-movies-sql-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas sqlalchemy requests
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 8 community components into src/$PKG/components/ + defs/"
$CLI add archive_fetcher       --auto-install
$CLI add csv_file_ingestion    --auto-install
$CLI add summarize             --auto-install
$CLI add formula               --auto-install
$CLI add filter                --auto-install
$CLI add dataframe_join        --auto-install
$CLI add sort                  --auto-install
$CLI add regex_parser          --auto-install
$CLI add dataframe_to_table    --auto-install

# Suppress the auto-installed example defs that would conflict with the demo
rm -rf "src/$PKG/defs/archive_fetcher" "src/$PKG/defs/csv_file_ingestion" \
       "src/$PKG/defs/summarize" "src/$PKG/defs/formula" \
       "src/$PKG/defs/filter" "src/$PKG/defs/dataframe_join" \
       "src/$PKG/defs/sort" "src/$PKG/defs/regex_parser" \
       "src/$PKG/defs/dataframe_to_table"

# Each pipeline step gets its own defs/ dir
for d in ml_archive movies_raw ratings_raw ratings_agg ratings_bayes \
         ratings_qualifying movies_scored top_250 top_250_with_year \
         movies_final movies_table; do
  mkdir -p "src/$PKG/defs/$d"
done

echo ">>> Writing demo defs.yaml"

# --- 1. archive_fetcher: download + extract MovieLens latest-small.zip
cat > "src/$PKG/defs/ml_archive/defs.yaml" <<EOF
type: $PKG.components.archive_fetcher.component.ArchiveFetcherComponent
attributes:
  asset_name: ml_archive
  url: https://files.grouplens.org/datasets/movielens/ml-latest-small.zip
  extract_to: /tmp/movies_demo
  flatten: true                # strip the top-level "ml-latest-small/" dir
  include_glob: ["*.csv"]      # only emit CSVs in the dict
  description: MovieLens (latest-small) — 100k ratings on 9k movies by 600 users
  group_name: ingest
EOF

# --- 2. movies_raw: read movies.csv after archive extraction
cat > "src/$PKG/defs/movies_raw/defs.yaml" <<EOF
type: $PKG.components.csv_file_ingestion.component.CSVFileIngestionComponent
attributes:
  asset_name: movies_raw
  file_path: /tmp/movies_demo/movies.csv
  deps: [ml_archive]
  description: Movies catalog (movieId, title, genres)
  group_name: ingest
EOF

# --- 3. ratings_raw: read ratings.csv after archive extraction
cat > "src/$PKG/defs/ratings_raw/defs.yaml" <<EOF
type: $PKG.components.csv_file_ingestion.component.CSVFileIngestionComponent
attributes:
  asset_name: ratings_raw
  file_path: /tmp/movies_demo/ratings.csv
  deps: [ml_archive]
  description: Per-user movie ratings (userId, movieId, rating, timestamp)
  group_name: ingest
EOF

# --- 4. ratings_agg: groupby movieId — avg_rating + num_ratings via named aggregations
cat > "src/$PKG/defs/ratings_agg/defs.yaml" <<EOF
type: $PKG.components.summarize.component.SummarizeComponent
attributes:
  asset_name: ratings_agg
  upstream_asset_key: ratings_raw
  group_by: [movieId]
  aggregations:
    avg_rating:  {col: rating, agg: mean}
    num_ratings: {col: rating, agg: count}
  description: Per-movie mean rating and total vote count
  group_name: transform
EOF

# --- 5. ratings_bayes: Bayesian-smoothed score
# bayes = (n/(n+m)) * avg_rating + (m/(n+m)) * C
# where m = 50 (min-ratings threshold), C ≈ 3.54 (global mean across ml-latest-small)
# Low-vote titles get pulled toward the global mean — same idea IMDB uses for its Top 250.
cat > "src/$PKG/defs/ratings_bayes/defs.yaml" <<EOF
type: $PKG.components.formula.component.FormulaComponent
attributes:
  asset_name: ratings_bayes
  upstream_asset_key: ratings_agg
  expressions:
    bayes: "(num_ratings / (num_ratings + 50)) * avg_rating + (50 / (num_ratings + 50)) * 3.54"
  description: Bayesian-smoothed rating per movie
  group_name: transform
EOF

# --- 6. ratings_qualifying: keep only movies with >= 50 ratings (per IMDB-style min vote count)
cat > "src/$PKG/defs/ratings_qualifying/defs.yaml" <<EOF
type: $PKG.components.filter.component.FilterComponent
attributes:
  asset_name: ratings_qualifying
  upstream_asset_key: ratings_bayes
  condition: "num_ratings >= 50"
  description: Movies with at least 50 ratings (qualifying threshold)
  group_name: transform
EOF

# --- 7. movies_scored: join movies catalog with the qualifying-bayes scores
cat > "src/$PKG/defs/movies_scored/defs.yaml" <<EOF
type: $PKG.components.dataframe_join.component.DataframeJoin
attributes:
  asset_name: movies_scored
  left_asset_key: movies_raw
  right_asset_key: ratings_qualifying
  how: inner
  "on": [movieId]   # quoted to defeat YAML 1.1's `on`-as-boolean interpretation
  description: Movies with titles, genres, and their Bayesian score
  group_name: transform
EOF

# --- 8. top_250: sort by bayes desc, take the top 250 in one shot
cat > "src/$PKG/defs/top_250/defs.yaml" <<EOF
type: $PKG.components.sort.component.SortComponent
attributes:
  asset_name: top_250
  upstream_asset_key: movies_scored
  by: [bayes]
  ascending: false
  limit: 250
  description: Top 250 movies by Bayesian-smoothed rating
  group_name: rank
EOF

# --- 9. top_250_with_year: pull the YYYY out of the "Title (YYYY)" suffix
cat > "src/$PKG/defs/top_250_with_year/defs.yaml" <<EOF
type: $PKG.components.regex_parser.component.RegexParser
attributes:
  asset_name: top_250_with_year
  upstream_asset_key: top_250
  column: title
  pattern: "\\\\((\\\\d{4})\\\\)\$"
  mode: extract
  output_columns: [year]
  group_name: rank
EOF

# --- 10. movies_final: clean title, compute decade, attach rank (1..250), pick output cols
cat > "src/$PKG/defs/movies_final/defs.yaml" <<EOF
type: $PKG.components.formula.component.FormulaComponent
attributes:
  asset_name: movies_final
  upstream_asset_key: top_250_with_year
  expressions:
    year_int:    "year.astype(int)"
    title_clean: 'title.str.replace(r"\\s*\\(\\d{4}\\)\$", "", regex=True)'
    decade:      "(year_int // 10) * 10"
    rank:        "df.index + 1"
    rating:      "avg_rating.round(2)"
  drop_source_columns: [year, title, avg_rating]
  description: Final shape — rank, title, year, decade, rating, num_ratings, genres
  group_name: rank
EOF

# --- 11. movies_table: write to SQLite via SQLAlchemy
cat > "src/$PKG/defs/movies_table/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_table.component.DataframeToTableComponent
attributes:
  asset_name: movies_table
  upstream_asset_key: movies_final
  table_name: top_movies
  database_url_env_var: DATABASE_URL
  if_exists: replace
  drop_timezone: true
  description: SQLite landing table for the Top-250
  group_name: sink
EOF

cat <<MSG

>>> Setup complete. 100% components — no inline Python in defs/.

Materialize headlessly (point DATABASE_URL at a SQLite file):
    cd $PROJECT_DIR
    DATABASE_URL=sqlite:////tmp/movies.db uv run dg launch --assets '*'

Or open the UI:
    DATABASE_URL=sqlite:////tmp/movies.db uv run dg dev

Inspect the result:
    sqlite3 /tmp/movies.db <<SQL
      .headers on
      .mode column
      SELECT decade, COUNT(*) AS movies, ROUND(AVG(rating), 2) AS avg_rating
      FROM top_movies GROUP BY decade ORDER BY decade DESC;
    SQL

You'll see the MovieLens Top 250 grouped by release decade, with average
rating per decade. Same query works against any backend — flip DATABASE_URL
to postgresql://… or mysql://… and the same pipeline lands data there instead.
MSG
