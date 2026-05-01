# Titanic — descriptive analytics

A 4-component pipeline that pulls a public Titanic CSV, filters to first-class
passengers, summarizes survival rate by gender, writes a CSV report.

## Pipeline

```
csv_file_ingestion → filter → summarize → dataframe_to_csv
```

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `csv_file_ingestion` | ingestion | Pull Titanic CSV from a public URL |
| 2 | `filter` | transformation | Keep only first-class passengers (`Pclass == 1`) |
| 3 | `summarize` | transformation | Group by `Sex`, compute survival rate |
| 4 | `dataframe_to_csv` | sink | Write `/tmp/survival_report.csv` |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_titanic_demo.sh | bash
cd titanic-demo
uv run dg launch --assets '*'
```

## Output

```
Sex     Survived    PassengerId
female  0.968       94
male    0.369       122
```

## What this demo shows

- The simplest end-to-end shape: ingest, transform, sink.
- The `dagster-component add` flow installs a working pipeline with one command per component.
- The schema-aware YAML autocomplete is enabled automatically (no editor config).
