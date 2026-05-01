# Vintage Cars → SQL — write a DataFrame to SQLite

A 4-component pipeline that pulls the classic vega cars dataset (406 cars from
1970-1982, US/Europe/Japan), parses the model year, derives a decade column,
and writes the result to a local SQLite database. No external DB needed — just
a file path in `DATABASE_URL`.

## Pipeline

```
rest_api_fetcher → datetime_parser → formula → dataframe_to_table
```

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `rest_api_fetcher` | ingestion | GET vega's cars.json (406-record JSON array, no auth) |
| 2 | `datetime_parser` | transformation | Parse the `Year` string ("YYYY-01-01") into a real datetime |
| 3 | `formula` | transformation | Compute `decade = (model_year.dt.year // 10) * 10` |
| 4 | `dataframe_to_table` | sink | Write to whatever DB `DATABASE_URL` points at — SQLite for this demo |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_cars_sql_demo.sh | bash
cd cars-sql-demo
DATABASE_URL=sqlite:////tmp/cars.db uv run dg launch --assets '*'
```

## Output

```
sqlite3 -header -column /tmp/cars.db <<SQL
  SELECT decade, Origin,
         COUNT(*)                        AS n_cars,
         ROUND(AVG(Miles_per_Gallon), 1) AS avg_mpg,
         ROUND(AVG(Horsepower), 0)        AS avg_hp
  FROM cars GROUP BY decade, Origin ORDER BY decade DESC, n_cars DESC;
SQL

decade  Origin  n_cars  avg_mpg  avg_hp
1980    USA     40      28.2     86.0
1980    Japan   34      34.4     77.0
1980    Europe  16      36.1     71.0
1970    USA     214     18.5     126.0
1970    Europe  57      25.6     83.0
1970    Japan   45      27.5     82.0
```

US cars went from 18.5 → 28.2 MPG / 126 → 86 HP across the decade. Europe and
Japan stayed efficient throughout.

## What this demo shows

- **First demo with a SQL sink.** All other demos write CSV / JSON / Parquet /
  Excel files. `dataframe_to_table` writes to anything SQLAlchemy supports —
  SQLite, Postgres, MySQL, Redshift, Snowflake, etc. Same `defs.yaml`, same
  pipeline; just change the `DATABASE_URL`.
- **Env-var-driven connection strings.** Database credentials live outside the
  pipeline definition. Useful in production where the same DAG points at dev
  vs. prod by toggling one env var.
- **`drop_timezone: true`** — datetime_parser produces a tz-aware column, but
  SQLite has no native tz storage. The sink auto-strips on the way in
  (preserving UTC wall time), logging which columns it touched. Set
  `false` if you're targeting Postgres TIMESTAMPTZ and want to keep tz.
