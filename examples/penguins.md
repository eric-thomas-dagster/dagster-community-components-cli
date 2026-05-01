# Palmer Penguins — ML feature engineering

A 5-component pipeline that pulls Palmer Penguins data, fills missing values,
one-hot encodes categorical columns, standard-scales numeric features, and
writes the ML-ready feature matrix to Parquet.

## Pipeline

```
csv_file_ingestion → imputation → one_hot_encoding → feature_scaler → dataframe_to_parquet
```

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `csv_file_ingestion` | ingestion | Pull penguins CSV |
| 2 | `imputation` | transformation | Fill missing numeric values (mean), categorical (mode) |
| 3 | `one_hot_encoding` | transformation | Expand `island`, `sex`, `species` into binary columns |
| 4 | `feature_scaler` | transformation | StandardScaler on the four numeric measurements |
| 5 | `dataframe_to_parquet` | sink | Write `/tmp/penguins_features.parquet` |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_penguins_demo.sh | bash
cd penguins-demo
uv run dagster asset materialize --select '*' -m definitions
```

## Output

`/tmp/penguins_features.parquet` — 344 rows × ~14 columns (4 scaled
numeric features + ~10 one-hot indicator columns).

## What this demo shows

- A canonical ML preprocessing chain assembled from off-the-shelf components.
- Different category mix from Titanic — focus is on transforms, not analytics.
- Parquet preserves dtypes; the downstream model trainer gets float32 columns
  with no type coercion at load time.
