# Wine ML Pipeline demo — single-file Python variant

> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

**Same pipeline as [`wine_ml_pipeline.md`](wine_ml_pipeline.md), one Python file, no YAML.** Community components can be defined in *either* YAML (via `defs.yaml`) *or* pure Python by instantiating the class directly. This walkthrough shows the Python shape — useful for teams migrating from single-script frameworks (Prefect, Airflow decorators, Luigi) who prefer everything visible in one file.

## Pipeline

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

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_wine_ml_pipeline_py_demo.sh | bash
cd wine-ml-pipeline-py-demo
uv run dg dev            # → http://localhost:3000 — click "Materialize all"
```

## Step-by-step — what the script does

If you'd rather build it by hand, here's the sequence — takes ~2 minutes.

### 1. Scaffold a Dagster project

```bash
uvx create-dagster@latest project wine-ml-pipeline-py-demo --no-uv-sync
cd wine-ml-pipeline-py-demo
```

You get a canonical Dagster project skeleton (`pyproject.toml`, `src/<pkg>/`, `src/<pkg>/defs/`, `pyproject.toml` with `[tool.dg.project]`).

### 2. Add dependencies

```bash
uv add pandas scikit-learn requests dagster-community-components
uv add --dev dagster-dg-cli dagster-webserver
```

`dagster-community-components` is the entire community library (~950 reusable components). `pandas` + `scikit-learn` power the ML steps this pipeline uses. `dagster-webserver` is the local UI.

### 3. Drop in the single-file pipeline

Replace the default `src/<pkg>/defs/__init__.py` (or write to `src/<pkg>/defs/pipeline.py`) with the file below. **The whole pipeline is one Python file — 9 component instances, wired by upstream_asset_key strings, merged into a single `Definitions` at the bottom.**

```python
"""Wine ML pipeline — pure Python variant.

Uses the same six community components as the sibling YAML walkthrough
(`wine_ml_pipeline`) but wires them via direct class instantiation.
"""
from dagster import Definitions
from dagster_community_components import (
    FileIngestionComponent,
    FeatureScalerComponent,
    CreateSamplesComponent,
    DecisionTreeModelComponent,
    CrossValidationComponent,
    DataframeToCsvComponent,
)

# The 11 chemistry features. Shared by scaler, model, and CV configs.
FEATURES = [
    "fixed acidity", "volatile acidity", "citric acid", "residual sugar",
    "chlorides", "free sulfur dioxide", "total sulfur dioxide", "density",
    "pH", "sulphates", "alcohol",
]

components = [
    # 1. Ingest — pull UCI red wine quality dataset from a public URL.
    FileIngestionComponent(
        asset_name="wine_raw",
        file_path="https://archive.ics.uci.edu/ml/machine-learning-databases/wine-quality/winequality-red.csv",
        description="UCI red wine quality dataset (1599 rows, 11 chemistry features).",
        delimiter=";",
        group_name="ingest",
    ),

    # 2. Standardize chemistry features → zero mean, unit variance.
    FeatureScalerComponent(
        asset_name="wine_scaled",
        upstream_asset_key="wine_raw",
        strategy="standard",
        columns=FEATURES,
        group_name="transform",
    ),

    # 3. 80/20 train/test split, stratified on quality.
    CreateSamplesComponent(
        asset_name="wine_split",
        upstream_asset_key="wine_scaled",
        test_size=0.2,
        random_state=42,
        stratify_column="quality",
        output_split_column="split",
        group_name="transform",
    ),

    # 4. Model — predictions branch.
    DecisionTreeModelComponent(
        asset_name="wine_predictions",
        upstream_asset_key="wine_split",
        target_column="quality",
        feature_columns=FEATURES,
        task_type="classification",
        max_depth=6,
        random_state=42,
        output_mode="predictions",
        group_name="model",
    ),

    # 5. Model — feature-importance branch (same fit, different output).
    DecisionTreeModelComponent(
        asset_name="wine_feature_importance",
        upstream_asset_key="wine_split",
        target_column="quality",
        feature_columns=FEATURES,
        task_type="classification",
        max_depth=6,
        random_state=42,
        output_mode="feature_importance",
        group_name="model",
    ),

    # 6. Cross-validation on the pre-split, scaled data (independent branch).
    CrossValidationComponent(
        asset_name="wine_cv_scores",
        upstream_asset_key="wine_scaled",
        target_column="quality",
        feature_columns=FEATURES,
        model_type="decision_tree",
        task_type="classification",
        cv_folds=5,
        random_state=42,
        group_name="validation",
    ),

    # 7-9. Three CSV sinks — same class, three instances, different upstream/path.
    DataframeToCsvComponent(
        asset_name="predictions_report",
        upstream_asset_key="wine_predictions",
        file_path="/tmp/wine_predictions.csv",
        group_name="sink",
    ),
    DataframeToCsvComponent(
        asset_name="importance_report",
        upstream_asset_key="wine_feature_importance",
        file_path="/tmp/wine_importance.csv",
        group_name="sink",
    ),
    DataframeToCsvComponent(
        asset_name="cv_report",
        upstream_asset_key="wine_cv_scores",
        file_path="/tmp/wine_cv.csv",
        group_name="sink",
    ),
]

# Merge each component's Definitions into one top-level Definitions.
# `Definitions.merge(*)` combines assets, asset_checks, sensors, schedules,
# jobs, and resources without duplication.
defs = Definitions.merge(*[c.build_defs(None) for c in components])
```

### 4. Run

```bash
uv run dg dev            # → http://localhost:3000
```

In the UI: click **Materialize all**. Watch the pipeline run:

- `wine_raw` fetches the CSV
- `wine_scaled` standardizes the 11 features
- `wine_split` splits 80/20
- `wine_predictions`, `wine_feature_importance` train a decision tree (two output branches)
- `wine_cv_scores` runs 5-fold cross-validation in parallel
- Three CSV sinks write the outputs to `/tmp/`

Or run headless:

```bash
uv run dg launch --assets '*'
ls -la /tmp/wine_*.csv
```

## When to use Python vs YAML

| Prefer Python | Prefer YAML |
|---|---|
| Team is used to script-shaped pipelines | Team is used to declarative config (dbt, Terraform, Kubernetes) |
| Component configs share values (e.g. a `FEATURES` list) — avoid stringly-typed repetition | Configs are heterogeneous, per-component |
| One-file examples / demos / notebooks | Component-per-file for git-diff scoping |
| Programmatic construction (loop over 100 config dicts) | Human-edited, one asset per YAML file |

Both shapes call the *same* component classes with the *same* validation. Mix them freely — you can have some components declared in Python and others in YAML in the same project.

## See also

- [`wine_ml_pipeline.md`](wine_ml_pipeline.md) — same pipeline, YAML shape (per-component `defs.yaml`).
- [`titanic_complete.md`](titanic_complete.md) — larger ML pipeline (12 components) on the Titanic dataset.
- [Walkthrough index](README.md) — 270+ end-to-end demos across every component family.
