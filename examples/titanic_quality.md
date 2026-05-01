# Titanic — data quality pipeline

A 5-component pipeline that takes the same Titanic CSV as the simplest demo
but routes it through three data-quality steps: cleanse strings, dedupe rows,
winsorize fare outliers. Demonstrates the kind of cleanup every real dataset
needs before downstream analytics.

## Pipeline

```
csv_file_ingestion → data_cleansing → unique_dedup → outlier_clipper → dataframe_to_csv
```

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `csv_file_ingestion` | ingestion | Pull Titanic CSV (known to have missing Age / Cabin / Embarked + Fare outliers) |
| 2 | `data_cleansing` | transformation | Trim whitespace, fill nulls in string columns with `"unknown"` |
| 3 | `unique_dedup` | transformation | Dedupe on `PassengerId`, keep first |
| 4 | `outlier_clipper` | transformation | Winsorize the `Fare` column to the IQR ×1.5 whiskers |
| 5 | `dataframe_to_csv` | sink | Write the cleaned 891-passenger frame |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_titanic_quality_demo.sh | bash
cd titanic-quality-demo
uv run dg launch --assets '*'
```

## Output

`/tmp/titanic_clean.csv` — 891 passengers, no duplicates, string nulls
filled, Fare outliers clipped. Compare the max `Fare` against the raw
data: it drops from `$512.33` (first-class outlier) to ~`$66` (the
1.5×IQR upper whisker).

```
PassengerId,Survived,Pclass,Name,Sex,Age,SibSp,Parch,Ticket,Fare,Cabin,Embarked
1,0,3,"Braund, Mr. Owen Harris",male,22.0,1,0,A/5 21171,7.25,unknown,S
2,1,1,"Cumings, Mrs. John Bradley...",female,38.0,1,0,PC 17599,65.6344,C85,C
```

(Cabin "unknown" instead of NaN; Fare 65.63 instead of the original 71.28
for Mrs. Cumings — clipped.)

## What this demo shows

- **A composable cleanup pipeline.** Each step is a single component with
  ~5-10 lines of YAML; chain them however the dataset needs.
- **`output_mode: unique` vs other modes.** `unique_dedup` also supports
  `duplicates` (return only the dupe rows) and `all` (flag dupes with a
  boolean column). Same component, different output.
- **`strategy: iqr` outlier handling.** `outlier_clipper` also supports
  `zscore` (threshold-based) and `quantile` (custom percentiles). The
  `action` toggles between `clip` (winsorize), `drop` (remove outlier
  rows), and `flag` (add a boolean column).
- **No `definitions.py`.** All five components autoload from
  `src/<pkg>/defs/`, no Python glue.

## Extending

Add `schema_validator` upstream to enforce a JSON Schema before processing
(reject rows that don't conform), or `imputation` instead of
`data_cleansing` if you want statistical fills (mean / median / mode)
rather than literal string fills.
