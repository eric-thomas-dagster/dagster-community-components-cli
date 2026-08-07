# ⭐ Wine ML Pipeline demo — one `MLPipelineComponent`

> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

> 🧭 **Not sure which variant to pick?** See [`wine_ml.md`](wine_ml.md) — the shape-selector index for all wine variants.

## This is what components were built for

**One YAML file. Whole ML pipeline. Standardized across the entire org.**

The other 7 wine variants show every way you *could* wire an ML pipeline in Dagster — from raw `@dg.asset` funcs to 9 separate `defs.yaml` files. All of them work. All of them are legitimate. **And every team will pick a different shape.** That's a governance problem: when marketing's ML pipeline looks nothing like supply-chain's ML pipeline looks nothing like fraud's ML pipeline, reviews take longer, CI gets bespoke per team, and onboarding a new hire means learning eight different wiring conventions.

**`MLPipelineComponent` fixes that.** One component in one YAML file per ML pipeline. Sibling of `polars_pipeline`, `warehouse_pipeline`, `pyspark_pipeline`, and `snowpark_pipeline` — the "pipeline component" family. Every ML pipeline in the org uses the same shape:

- **Same schema** — `source` + `steps` + `outputs`. Reviewers scan a fixed layout.
- **Same validation** — CI runs one schema check against every pipeline.
- **Same enforcement** — a linter can require certain steps (e.g. "every pipeline must have a `cross_validate` step") uniformly.
- **Same onboarding** — a new hire reads one component doc and can build any pipeline.

That's the value proposition of components. Not "reduce lines of code" — **encode a pattern that scales across teams and time**.

## Pipeline

```
source (URL) ──▶ [scale → split → train → predict + importance + cross_validate] ──▶ 3 CSVs
              (all inside one MLPipelineComponent-emitted multi_asset)
```

**3 asset outputs from ONE component**: `wine_ml_preds`, `wine_ml_imp`, `wine_ml_cv`.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_wine_ml_pipeline_component_demo.sh | bash
cd wine-ml-pipeline-component-demo
uv run dg dev            # → http://localhost:3000 — click "Materialize all"
```

## The YAML — one file, whole pipeline

Write to `src/<pkg>/defs/wine_ml/defs.yaml`:

```yaml
type: dagster_community_components.MLPipelineComponent
attributes:
  asset_name_prefix: wine_ml
  group_name: ml

  source:
    kind: url
    url: "https://archive.ics.uci.edu/ml/machine-learning-databases/wine-quality/winequality-red.csv"
    delimiter: ";"

  target_column: quality
  feature_columns:
    - "fixed acidity"
    - "volatile acidity"
    - "citric acid"
    - "residual sugar"
    - "chlorides"
    - "free sulfur dioxide"
    - "total sulfur dioxide"
    - "density"
    - "pH"
    - "sulphates"
    - "alcohol"

  steps:
    - {id: scaled,  op: scale, method: standard}
    - {id: split,   op: split, test_size: 0.2, stratify_column: quality, random_state: 42}
    - {id: trained, op: train, model_type: decision_tree, task_type: classification,
                    params: {max_depth: 6, random_state: 42}}
    - {id: preds,   op: predict, model: trained, input: scaled}
    - {id: imp,     op: importance, model: trained}
    - {id: cv,      op: cross_validate, source: scaled,
                    model_type: decision_tree, task_type: classification,
                    params: {max_depth: 6, random_state: 42}, cv: 5}

  outputs:
    assets: [preds, imp, cv]
    csv_sinks:
      - {from: preds, path: /tmp/wine_predictions.csv}
      - {from: imp,   path: /tmp/wine_importance.csv}
      - {from: cv,    path: /tmp/wine_cv.csv}
```

**That's the whole pipeline.** Compare to [`wine_ml_pipeline.md`](wine_ml_pipeline.md) which needs 9 separate `defs.yaml` files.

## Source options — file, URL, warehouse, or upstream asset

`source:` accepts four kinds:

| kind | Config | Use case |
|---|---|---|
| `url` | `{kind: url, url: ..., delimiter: ","}` | Public datasets, remote CSVs |
| `file` | `{kind: file, path: ..., delimiter: ","}` | Local CSV in the project dir or mounted volume |
| `warehouse_query` | `{kind: warehouse_query, resource_key: snowflake, sql: "SELECT ..."}` | **Production ML from a live warehouse** — Snowflake, BigQuery, Postgres, MySQL, MSSQL |
| `upstream_asset` | `{kind: upstream_asset, upstream_asset_key: raw/events}` | Chain the ML pipeline downstream of any other Dagster asset |

The `warehouse_query` kind works with **any Dagster resource** that exposes `.get_engine()` (SQLAlchemy) or `.get_connection()` (DB-API) — the community-components `postgres_resource`, `mysql_resource`, `mssql_resource`, `snowflake_resource`, `bigquery_resource`, or any custom resource with the same shape. **Required resource keys are auto-detected** from source + sink configs — the customer never lists them.

## Output options — csv, parquet, table, or asset

`outputs:` accepts four sink kinds (all optional; `assets:` is required):

```yaml
outputs:
  assets: [preds, imp, cv]                  # step outputs → first-class Dagster assets
  csv_sinks:
    - {from: preds, path: /tmp/preds.csv}
  parquet_sinks:
    - {from: preds, path: s3://ml-out/preds.parquet}
  table_sinks:
    - {from: preds, resource_key: snowflake, table: ml_predictions,
       schema: analytics, if_exists: replace}
```

Same resource-key shape as `warehouse_query`. Same auto-detection of required resources.

## The op menu

`MLPipelineComponent` reimplements the core ML ops so it's self-contained (no cross-component delegation). If you need something not listed, use `sklearn_class:` (see below).

**Feature engineering:**
- `scale` — StandardScaler / MinMaxScaler / RobustScaler
- `impute` — mean / median / mode / constant
- `one_hot_encode` — pandas `get_dummies` on named columns
- `label_encode` — LabelEncoder on categoricals
- `tile_binning` — pandas `qcut` into N quantile bins
- `outlier_clip` — IQR-based clipping
- `filter` — pandas `.query()` predicate
- `select` — column subset
- `date_features` — extract year / month / day / weekday / hour from a datetime column
- `polynomial_features` — sklearn PolynomialFeatures
- `pca` — dimensionality reduction with explained-variance logging

**Train / test / evaluate:**
- `split` — sklearn `train_test_split`, adds a `split` column (train/test or train/val/test)
- `train` — fit any model_type (see below)
- `predict` — model.predict on any DataFrame
- `predict_proba` — model.predict_proba (classification)
- `importance` — feature_importances_ or |coef_|
- `cross_validate` — sklearn `cross_validate` with per-fold scores

## Model support — first-class + escape hatch

**First-class enum values** (`model_type: ...`, task-aware):
- `decision_tree` — sklearn DecisionTreeClassifier / DecisionTreeRegressor
- `random_forest` — RandomForestClassifier / RandomForestRegressor
- `gradient_boosting` — GradientBoostingClassifier / GradientBoostingRegressor
- `logistic_regression` — LogisticRegression (classification only)
- `kmeans` — KMeans (clustering)

**Escape hatch** — `sklearn_class:` accepts any dotted path to an estimator with `.fit` / `.predict`:

```yaml
- {id: trained, op: train, sklearn_class: "sklearn.ensemble.HistGradientBoostingClassifier",
                task_type: classification, params: {max_iter: 200, learning_rate: 0.1}}

- {id: trained, op: train, sklearn_class: "xgboost.XGBClassifier",
                task_type: classification, params: {n_estimators: 500, max_depth: 8}}

- {id: trained, op: train, sklearn_class: "lightgbm.LGBMRegressor",
                task_type: regression, params: {num_leaves: 63, learning_rate: 0.05}}
```

**Params are forwarded to the estimator constructor as-is.** No component update needed to unlock a new model or param.

## Production example — Snowflake in, Snowflake out

The wine walkthrough uses a URL source and CSV sinks for portability. Real production ML usually pulls from a warehouse and writes predictions back. **Same component, different `source:` and `outputs:`:**

```yaml
type: dagster_community_components.MLPipelineComponent
attributes:
  asset_name_prefix: customer_churn

  source:
    kind: warehouse_query
    resource_key: snowflake          # auto-added to required_resource_keys
    sql: |
      SELECT customer_id, tenure_months, monthly_charges, total_charges,
             support_tickets, is_active, churned
      FROM analytics.customer_features
      WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM analytics.customer_features)

  target_column: churned
  feature_columns: [tenure_months, monthly_charges, total_charges, support_tickets, is_active]

  steps:
    - {id: imputed, op: impute, strategy: median}
    - {id: scaled,  op: scale, method: standard}
    - {id: split,   op: split, test_size: 0.2, stratify_column: churned, random_state: 42}
    - {id: trained, op: train, sklearn_class: "xgboost.XGBClassifier",
                    task_type: classification, params: {n_estimators: 500, max_depth: 6}}
    - {id: preds,   op: predict_proba, model: trained, input: scaled}
    - {id: imp,     op: importance, model: trained}
    - {id: cv,      op: cross_validate, source: scaled,
                    sklearn_class: "xgboost.XGBClassifier",
                    task_type: classification, params: {n_estimators: 500, max_depth: 6}, cv: 5}

  outputs:
    assets: [preds, imp, cv]
    table_sinks:
      - {from: preds, resource_key: snowflake, table: churn_predictions,
         schema: ml_output, if_exists: replace}
      - {from: imp,   resource_key: snowflake, table: churn_feature_importance,
         schema: ml_output, if_exists: replace}
```

Wire `snowflake` in the top-level `definitions.py` as a `SnowflakeResource` or the community `snowflake_resource`. That's it — the component reads from Snowflake, runs XGBoost, writes predictions back to Snowflake.

## Standardization example — three org pipelines, same shape

```yaml
# marketing_conversion_ml.yaml
type: dagster_community_components.MLPipelineComponent
attributes:
  asset_name_prefix: marketing_conversion
  source: {kind: upstream_asset, upstream_asset_key: marketing/enriched_events}
  target_column: converted
  feature_columns: [ad_spend, sessions, email_opens, ...]
  steps:
    - {id: split,   op: split, test_size: 0.3, stratify_column: converted, random_state: 42}
    - {id: trained, op: train, model_type: gradient_boosting, task_type: classification, params: {n_estimators: 200}}
    - {id: preds,   op: predict, model: trained, input: source}
    - {id: cv,      op: cross_validate, source: source,
                    model_type: gradient_boosting, task_type: classification, params: {n_estimators: 200}, cv: 5}
  outputs:
    assets: [preds, cv]
    csv_sinks:
      - {from: preds, path: /out/marketing_predictions.csv}
```

```yaml
# supply_forecast_ml.yaml
type: dagster_community_components.MLPipelineComponent
attributes:
  asset_name_prefix: supply_forecast
  source: {kind: upstream_asset, upstream_asset_key: supply/weekly_shipments}
  target_column: units_shipped_next_week
  feature_columns: [units_shipped_last_week, avg_lead_time, seasonality_index, ...]
  steps:
    - {id: split,   op: split, test_size: 0.2, random_state: 42}
    - {id: trained, op: train, sklearn_class: "sklearn.ensemble.HistGradientBoostingRegressor",
                    task_type: regression, params: {max_iter: 500}}
    - {id: preds,   op: predict, model: trained, input: source}
    - {id: imp,     op: importance, model: trained}
  outputs:
    assets: [preds, imp]
```

**Same YAML shape** in both. New hires who learn one file learn every ML pipeline in the org.

## When to reach for MLPipelineComponent vs. the alternatives

| Reach for | When |
|---|---|
| **`MLPipelineComponent`** | You want ONE standardized ML pipeline shape across the whole org. Easy to review, validate, and enforce. |
| [`_yaml`](wine_ml_pipeline.md) (6 separate components) | You want per-stage components that each individual team can swap independently (e.g. team A uses `DecisionTreeModelComponent`; team B swaps in `RandomForestModelComponent`). |
| [`_py_minimal`](wine_ml_pipeline_py_minimal.md) | You want components for boilerplate stages (ingest) but custom Python for bespoke ML logic. |
| [`_minimal`](wine_ml_pipeline_minimal.md) | You want zero components; single-file Python. |

## See also

- [`wine_ml.md`](wine_ml.md) — the shape-selector index for all eight wine variants.
- [`wine_ml_pipeline.md`](wine_ml_pipeline.md) — same pipeline as 6 separate community components in YAML (contrast to this variant's single component).
- [`wine_ml_pipeline_py_minimal.md`](wine_ml_pipeline_py_minimal.md) — hybrid: 1 component + custom Python.
- Sibling pipeline components: [`warehouse_pipeline`](warehouse_pipeline.md), [`polars_pipeline`](polars_pipeline.md), [`pyspark_pipeline`](pyspark_pipeline.md), [`snowpark_pipeline`](snowpark_pipeline.md) — same "one YAML, multi-step, multi-output" pattern for other domains.
- [Walkthrough index](README.md) — 270+ end-to-end demos across every component family.
