# Titanic Complete demo


A larger companion to the focused titanic_demo / titanic_etl_demo /
titanic_logreg_demo / titanic_quality_demo demos. This one walks the
whole journey in a single pipeline:
  ingest → quality (dedup+cleanse+outliers) → ETL (impute, type-coerce,
  bin, one-hot) → model (logistic regression) → outputs (predictions,
  summary stats, survivors-only).

Pipeline (12 components, all autoloaded by `dg`):
                                                                     ┌─→ logistic_regression  → CSV
  csv_file_ingestion                                                  │
    → unique_dedup → data_cleansing → outlier_clipper                 │
    → imputation → type_coercer → tile_binning → one_hot_encoding ─┬─┘
                                                                   │
                                                                   ├─→ summarize (EDA)        → CSV
                                                                   │
                                                                   └─→ filter (survivors)     → CSV

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_titanic_complete_demo.sh | bash
cd titanic-complete-demo
uv run dg launch --assets '*'
```
