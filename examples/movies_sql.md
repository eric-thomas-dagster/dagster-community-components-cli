# Top 250 Movies → SQL demo

Computes a "Top 250" ranking from **real MovieLens data** (officially published
by GroupLens), parses year out of the title, and lands the result in SQLite via
`dataframe_to_table`. Set `DATABASE_URL` to `postgresql://…` or `mysql://…` and
the same pipeline lands data there instead — that's the point.

```
file_ingestion → type_coercer → formula → dataframe_to_table
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `file_ingestion` | ingestion | Read MovieLens-derived CSV |
| 2 | `type_coercer` | transformation | Coerce `year` → int, `rating` → float |
| 3 | `formula` | transformation | Compute `decade` from `year` |
| 4 | `dataframe_to_table` | sink | Write to SQLite via SQLAlchemy |

## Source data

The setup script downloads
[`ml-latest-small.zip`](https://files.grouplens.org/datasets/movielens/ml-latest-small.zip)
from grouplens.org (~1MB), joins `movies.csv` with `ratings.csv`, and computes a
**Bayesian-smoothed rating** to rank titles — same idea IMDB uses for their Top
250 (titles with too few votes get pulled toward the global mean):

```
bayes_rating = (n / (n + m)) * avg_rating + (m / (n + m)) * global_mean
```

with `m = 50` (minimum votes) and `n` = the title's vote count. Then top 250 by
`bayes_rating` flows into the pipeline.

The IMDB-curated Top 250 itself isn't published as a CSV by IMDB — third-party
mirrors come and go. MovieLens is officially hosted, stable, and produces a
defensibly-similar ranking from real user ratings.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_movies_sql_demo.sh | bash
cd movies-sql-demo
DATABASE_URL=sqlite:////tmp/movies.db uv run dg launch --assets '*'
```

## Inspect

```bash
sqlite3 /tmp/movies.db <<SQL
.headers on
.mode column
SELECT decade, COUNT(*) AS movies, ROUND(AVG(rating), 2) AS avg_rating
FROM top_movies GROUP BY decade ORDER BY decade DESC;
SQL
```

You'll see how the Top 250 distribute by release decade and how their average
ratings move over time.
