# Wine ML Pipeline demo — one `MLPipelineComponent`

> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

> 🧭 **Not sure which variant to pick?** See [`wine_ml.md`](wine_ml.md) — the shape-selector index for all wine variants.

**Eighth variant of the wine pipeline** — same 6-step logic collapsed into **one YAML file** using `MLPipelineComponent`, the community-components entry in the same "pipeline component" family as `polars_pipeline`, `warehouse_pipeline`, `pyspark_pipeline`, and `snowpark_pipeline`.

## Why this shape

The other component variants (`_py`, `_yaml`, `_py_minimal`) each use 6 different components (`FileIngestionComponent`, `FeatureScalerComponent`, `CreateSamplesComponent`, `DecisionTreeModelComponent`, `CrossValidationComponent`, `DataframeToCsvComponent`) plus wiring between them. That's flexible but non-standard — every ML pipeline in the org looks slightly different depending on which components the team picked.

**`MLPipelineComponent` standardizes the shape.** One YAML file per ML pipeline. Every ML pipeline in the org looks the same. Reviewers can scan the `steps:` block and know exactly what's happening. CI can validate against ONE schema. New team members read one component's docs and can build any ML pipeline.

**This is what companies mean when they ask for standardized patterns.** It's exactly what components were designed for.

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

## The op menu

`MLPipelineComponent` reimplements the core ML ops so it's self-contained (no cross-component delegation). Same coverage as the individual community components; if you need something not listed, use `sklearn_class:` (see below).

**Feature engineering:**
- `scale` — StandardScaler / MinMaxScaler / RobustScaler
- `impute` — mean / median / mode / constant
- `one_hot_encode` — pandas `get_dummies` on named columns
- `label_encode` — LabelEncoder on categoricals
- `tile_binning` — pandas `qcut` into N quantile bins
- `outlier_clip` — IQR-based clipping
- `filter` — pandas `.query()` predicate
- `select` — column subset

**Train / test / evaluate:**
- `split` — sklearn `train_test_split`, adds a `split` column
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
