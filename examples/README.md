# Examples

End-to-end Dagster pipelines built entirely from community components — no
custom Python beyond `model_validate({...})` calls. Each demo:

- Hits a **public dataset or API** (no auth, no API key).
- Installs every component via `dagster-component add`.
- Materializes a small but real pipeline you can run locally in under a minute.

| Demo | Pipeline | Highlights |
|---|---|---|
| [Titanic](titanic.md) | csv → filter → summarize → csv | The simplest end-to-end shape |
| [Palmer Penguins](penguins.md) | csv → impute → onehot → scale → parquet | Canonical ML preprocessing |
| [USGS Earthquakes](earthquakes.md) | rest → flatten → select → sort → json | REST + nested JSON |
| [Earthquakes (partitioned)](partitioned_earthquakes.md) | same, daily-partitioned | Backfillable date range |
| [SpaceX Launches](spacex.md) | rest → select → datetime → rank → excel | Datetime parsing + ranking |
| [REST Countries](countries.md) | rest → formula → summarize → json | Computed columns + rollup |
| [NYC Weather](weather.md) | rest → datetime → running_total → transpose → csv | Columnar API + cumulative + pivot |
| [Dagster GitHub Releases](releases.md) | rest → select → datetime → filter → sort → parquet | Filter + sort + parquet |
| [Wine Quality (ML)](wine.md) | csv → random_forest_model × 2 → csv | Train a real model + emit predictions and feature importance |

## Component coverage

Across the 9 demos, this hits **19 distinct components** in 5 categories:

- **ingestion** — `csv_file_ingestion`, `rest_api_fetcher`
- **transformation** — `filter`, `summarize`, `imputation`, `one_hot_encoding`, `feature_scaler`, `json_flatten`, `select_columns`, `sort`, `datetime_parser`, `rank`, `formula`, `running_total`, `transpose`
- **analytics** — `random_forest_model`
- **sink** — `dataframe_to_csv`, `dataframe_to_parquet`, `dataframe_to_json`, `dataframe_to_excel`

## How they're built

Each demo is a single Bash script (`setup_*.sh`) that:

1. `uvx create-dagster project <name>` — scaffolds a canonical Dagster project
2. `uv add`s any format-specific libs (pyarrow, openpyxl, etc.)
3. `dagster-component add <id> --auto-install`s each component into
   `src/<pkg>/defs/<id>/` (the CLI auto-detects the canonical layout)
4. Writes a `defs.yaml` per component with demo-specific attributes —
   `dg`'s autoloader picks them up; no `definitions.py` glue
5. Prints the run command (`dg launch --assets '*'`) + an inspect snippet

Run any demo:

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_<name>_demo.sh | bash
cd <name>-demo
uv run dg launch --assets '*'
```

## Why these exist

Each demo doubles as an **integration test** that exercises a different
combination of source / transform / sink. Several real component bugs
(silent NaN on tz-aware datetimes in Excel, columnar dict misinterpretation
in the REST fetcher, silent expression failures in `multi_field_formula`)
were surfaced and fixed by the act of building these.
