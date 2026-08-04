# Titanic Complete demo
> ✅ **Dagster+ Serverless:** deploys as-is via `dagster-cloud serverless deploy-docker`.


A larger companion to the focused titanic_demo / titanic_etl_demo /
titanic_logreg_demo / titanic_quality_demo demos. This one walks the
whole journey in a single pipeline:
  ingest → quality (dedup+cleanse+outliers) → ETL (impute, type-coerce,
  bin, one-hot) → model (logistic regression) → outputs (predictions,
  summary stats, survivors-only).

Pipeline (12 components, all autoloaded by `dg`):
                                                                     ┌─→ logistic_regression  → CSV
  file_ingestion                                                  │
    → unique_dedup → data_cleansing → outlier_clipper                 │
    → imputation → type_coercer → tile_binning → one_hot_encoding ─┬─┘
                                                                   │
                                                                   ├─→ summarize (EDA)        → CSV
                                                                   │
                                                                   └─→ filter (survivors)     → CSV

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `file_ingestion` | ingestion | Read source CSV |
| 2 | `unique_dedup` | transformation | Drop duplicates |
| 3 | `data_cleansing` | transformation | Clean text fields |
| 4 | `outlier_clipper` | transformation | Clip IQR outliers |
| 5 | `imputation` | transformation | Fill missing values |
| 6 | `type_coercer` | transformation | Coerce column types |
| 7 | `tile_binning` | transformation | Bin a numeric column |
| 8 | `one_hot_encoding` | transformation | Expand categorical → dummies |
| 9 | `logistic_regression_model` | analytics | Fit logistic regression |
| 10 | `summarize` | transformation | Group-by aggregate |
| 11 | `filter` | transformation | Row filter by predicate |
| 12 | `dataframe_to_csv` | sink | Write CSV |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_titanic_complete_demo.sh | bash
cd titanic-complete-demo
uv run dg launch --assets '*'
```

## See also

<!-- TODO: link related walkthroughs -->
