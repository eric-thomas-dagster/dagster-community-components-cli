# BigQuery-native ML pipeline — CTAS + BQML train + BQML predict
> ✅ **Dagster+ Serverless:** deploys as-is via `dagster-cloud serverless deploy-docker`.

**A complete ML pipeline running entirely inside BigQuery** — no Python
ML stack, no scikit-learn, no PyTorch. Trains and predicts via SQL.
Validated live in ~1m45s end-to-end against the public iris dataset.

```
iris_clean              ← bigquery_create_table_from_query_asset
                          (CTAS from `bigquery-public-data.ml_datasets.iris`,
                          drops null species rows; partitioned + clustered as configured)
        │
        └── iris_logreg_model     ← bigquery_ml_train_asset
                                    (LOGISTIC_REG, AUTO_SPLIT, auto_class_weights)
                │
                └── iris_predictions   ← bigquery_ml_predict_asset
                                          (ML.PREDICT on first 10 rows)
                          │
                          └── iris_predictions_csv  ← dataframe_to_csv (/tmp/iris_predictions.csv)
```

## Components used

| Component | What it does |
|---|---|
| `bigquery_create_table_from_query_asset` | CTAS — `CREATE OR REPLACE TABLE/VIEW/MATERIALIZED VIEW` from a SELECT. Transform layer of any BQ-native ELT (same shape as a dbt model, run directly without dbt). |
| `bigquery_ml_train_asset` | Train a BQML model (LINEAR_REG, LOGISTIC_REG, KMEANS, ARIMA_PLUS, DNN_*, BOOSTED_TREE_*, AUTOML_*, etc.) in pure SQL. |
| `bigquery_ml_predict_asset` | Run ML.PREDICT / ML.FORECAST / ML.EXPLAIN_PREDICT / ML.DETECT_ANOMALIES against a trained model. |
| `dataframe_to_csv` | Local CSV export. (For Dagster+ Cloud, swap for `dataframe_to_bigquery` / `dataframe_to_gcs`.) |

## Validation status — all live

| Step | Run time | Result |
|---|---|---|
| `iris_clean` (CTAS) | 5.37s | Real BQ table at `servicepulse-490502.dagster_demo.iris_clean` |
| `iris_logreg_model` (BQML train) | **1m36s** | Real LOGISTIC_REG model trained in BigQuery |
| `iris_predictions` (BQML predict) | <2s | 10 prediction rows |
| `iris_predictions_csv` | <1s | `/tmp/iris_predictions.csv` |

Sample prediction (from the CSV):

```
predicted_species, predicted_species_probs (top), sepal_length, sepal_width, petal_length, petal_width
setosa,            setosa @ 99.92%,                4.6,           3.6,         1.0,          0.2
```

## Cost

**~$0.001.** Iris is 150 rows; CTAS scans <1 KB, training scans <1 KB,
prediction scans 10 rows. All below the BQ free tier in practice.

## Required env vars

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
```

The SA needs at least `roles/bigquery.dataEditor` on the destination
dataset + `roles/bigquery.jobUser` on the project. (Or simpler:
`roles/owner` for demos.)

## Run

1. Enable the **BigQuery API** on the SA's project.
2. Grant the SA the BQ roles (or `roles/owner`).
3. Create a dataset to materialize into (defaults to `servicepulse-490502.dagster_demo`):
   ```bash
   bq --location=US mk -d dagster_demo
   ```
   (or override with `BQ_DATASET=my-project.my_dataset` before running the script)

## Run it

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_bigquery_ml_pipeline_demo.sh | bash
cd bigquery-ml-pipeline-demo
uv run dg launch --assets '*'
```

Inspect:

```bash
# Local CSV
cat /tmp/iris_predictions.csv

# Cloud — verify the BQ table + model exist
bq show servicepulse-490502:dagster_demo.iris_clean
bq show -m servicepulse-490502:dagster_demo.iris_logreg

# Run an ad-hoc prediction
bq query --nouse_legacy_sql '
  SELECT predicted_species, sepal_length, sepal_width
  FROM ML.PREDICT(MODEL `servicepulse-490502.dagster_demo.iris_logreg`,
                  TABLE `servicepulse-490502.dagster_demo.iris_clean`)
  LIMIT 5'
```

## Why BQML vs. Python ML?

- **No Python ML stack required** — runs in any Dagster+ deployment without
  installing scikit-learn / PyTorch / TensorFlow / pandas-on-cluster, etc.
- **Train + predict scale natively in BigQuery** — same compute as your
  warehouse, no cluster spin-up.
- **Cheap** — first 10 GB of BQML training compute / month is free; iris
  costs effectively $0.
- **Reproducible** — the model definition is a SQL CREATE statement, fits
  into version control.

## Drop-in extensions

Swap `model_type` for any BQML-supported model:

```yaml
# Time-series forecasting (e.g., daily revenue)
model_options:
  model_type: ARIMA_PLUS
  time_series_timestamp_col: order_date
  time_series_data_col: revenue
  horizon: 30
  auto_arima: true

# K-means customer segmentation
model_options:
  model_type: KMEANS
  num_clusters: 5
  standardize_features: true

# Deep neural network classifier
model_options:
  model_type: DNN_CLASSIFIER
  input_label_cols: [churned]
  hidden_units: [64, 32, 16]
  activation_fn: RELU
  dropout: 0.2
```

For ARIMA forecasting, set the predict step's `operation` to `forecast`:

```yaml
operation: forecast
options:
  horizon: 30
  confidence_level: 0.95
```

## See also

<!-- TODO: link related walkthroughs -->
