# IMDB Top 250 → SQL demo


Pulls the IMDB Top 250 dataset (CSV from a public mirror, no auth),
parses the year as an int, computes a decade column, lands the result
in a local SQLite database via dataframe_to_table.

Pipeline (4 components, all autoloaded by `dg`):
    csv_file_ingestion → type_coercer → formula → dataframe_to_table

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_movies_sql_demo.sh | bash
cd movies-sql-demo
uv run dg launch --assets '*'
```
