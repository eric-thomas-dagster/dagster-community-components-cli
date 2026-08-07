# Wine ML Pipeline demo
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

> 🧭 **Not sure which variant to pick?** See [`wine_ml.md`](wine_ml.md) — the shape-selector index for all six wine variants with pros / cons and a decision tree.

Builds a more substantial ML pipeline than the existing wine_demo:
  - feature scaling (standardize the 11 chemistry features)
  - train/test split via create_samples
  - decision_tree_model with two parallel outputs (predictions + feature_importance)
  - cross_validation to verify model stability across folds
  - three CSV sinks

Pipeline (6 components, all autoloaded by `dg`):

```
file_ingestion
      │
      ▼
feature_scaler
      │
      ├──▶ create_samples ──▶ decision_tree_model ──┬──▶ CSV (predictions)
      │                                             └──▶ CSV (feature_importance)
      │
      └──▶ cross_validation ─────────────────────────────▶ CSV (cv_scores)
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `file_ingestion` | ingestion | Read source CSV |
| 2 | `feature_scaler` | transformation | Standardize features |
| 3 | `create_samples` | transformation | Train/test split |
| 4 | `decision_tree_model` | analytics | Fit decision tree |
| 5 | `cross_validation` | analytics | k-fold CV scores |
| 6 | `dataframe_to_csv` | sink | Write CSV |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_wine_ml_pipeline_demo.sh | bash
cd wine-ml-pipeline-demo
uv run dg launch --assets '*'
```

## See also

- [`wine_ml_pipeline_minimal.md`](wine_ml_pipeline_minimal.md) — same pipeline with only 3 assets (scaling/splitting/CSV writes are plain Python; most Prefect-like).
- [`wine_ml_pipeline_py.md`](wine_ml_pipeline_py.md) — same pipeline as one Python file (community components, no YAML).
- [`wine_ml_pipeline_raw.md`](wine_ml_pipeline_raw.md) — same pipeline as one Python file, no components (pure Dagster + inline).
- [`wine_ml_pipeline_ops.md`](wine_ml_pipeline_ops.md) — same pipeline with @op-decomposed model stage inside a @graph_multi_asset.
- [`wine_ml_pipeline_helpers.md`](wine_ml_pipeline_helpers.md) — same pipeline with plain-Python-helper decomposition (no @op, no graph).
- [`titanic_complete.md`](titanic_complete.md) — larger ML pipeline (12 components) on the Titanic dataset.
- [`airports_cluster.md`](airports_cluster.md) — unsupervised ML variant (k-means clustering).
- [Walkthrough index](README.md) — 270+ end-to-end demos across every component family.
