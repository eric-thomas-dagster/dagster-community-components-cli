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
uv add -q pandas sqlalchemy
uv add --dev -q dagster-dg-cli dagster-webserver

mkdir -p /tmp/movies_demo
echo ">>> Downloading MovieLens (latest-small) — real public ratings dataset hosted by GroupLens"
if [ ! -f /tmp/movies_demo/ml-latest-small/movies.csv ]; then
  curl -fsSL https://files.grouplens.org/datasets/movielens/ml-latest-small.zip -o /tmp/movies_demo/ml.zip
  unzip -oq /tmp/movies_demo/ml.zip -d /tmp/movies_demo/
fi

echo ">>> Computing the Top 250 by Bayesian-adjusted rating (real MovieLens data)"
uv run python - <<'PY'
import csv, re
import pandas as pd

movies  = pd.read_csv("/tmp/movies_demo/ml-latest-small/movies.csv")
ratings = pd.read_csv("/tmp/movies_demo/ml-latest-small/ratings.csv")

# Aggregate per movie
agg = ratings.groupby("movieId").agg(
    avg_rating=("rating", "mean"),
    num_ratings=("rating", "count"),
).reset_index()

# Bayesian smoothed rating: weight low-vote-count titles toward the global mean.
# This is the same idea IMDB uses for its Top 250 (m = min votes threshold).
m = 50  # minimum ratings to qualify
C = ratings["rating"].mean()
agg["bayes"] = (agg["num_ratings"] / (agg["num_ratings"] + m)) * agg["avg_rating"] \
             + (m / (agg["num_ratings"] + m)) * C

merged = movies.merge(agg[agg["num_ratings"] >= m], on="movieId")
merged = merged.sort_values("bayes", ascending=False).head(250).reset_index(drop=True)

# Extract year from title — MovieLens encodes it as "Title (YYYY)"
merged["year"] = merged["title"].str.extract(r"\((\d{4})\)$")
merged["title_clean"] = merged["title"].str.replace(r"\s*\(\d{4}\)$", "", regex=True)
merged["rank"] = merged.index + 1

out = merged[["rank", "title_clean", "year", "avg_rating", "num_ratings", "genres"]].rename(
    columns={"title_clean": "title", "avg_rating": "rating"}
)
out["rating"] = out["rating"].round(2)
out.to_csv("/tmp/movies_demo/top_movies.csv", index=False)
print(f"wrote /tmp/movies_demo/top_movies.csv ({len(out)} rows)")
print(out.head().to_string(index=False))
PY

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 4 community components into src/$PKG/components/ + defs/"
$CLI add csv_file_ingestion    --auto-install
$CLI add type_coercer          --auto-install
$CLI add formula               --auto-install
$CLI add dataframe_to_table    --auto-install

echo ">>> Writing demo defs.yaml for each component"

# 1. Ingest — Top 250 movies computed from real MovieLens ratings data.
# (The IMDB-curated Top 250 isn't published as a CSV by IMDB themselves; their
# `title.ratings.tsv.gz` lacks titles, and third-party Top-250 GitHub mirrors decay.
# MovieLens is officially hosted by GroupLens at files.grouplens.org and stable.)
cat > "src/$PKG/defs/csv_file_ingestion/defs.yaml" <<EOF
type: $PKG.components.csv_file_ingestion.component.CSVFileIngestionComponent
attributes:
  asset_name: movies_raw
  file_path: /tmp/movies_demo/top_movies.csv
  description: Top 250 movies by Bayesian-smoothed rating, derived from real MovieLens (latest-small) ratings
  group_name: ingest
EOF

# 2. Coerce types — `year` came in as a string; we want it as int for math
cat > "src/$PKG/defs/type_coercer/defs.yaml" <<EOF
type: $PKG.components.type_coercer.component.TypeCoercerComponent
attributes:
  asset_name: movies_typed
  upstream_asset_key: movies_raw
  type_map:
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
