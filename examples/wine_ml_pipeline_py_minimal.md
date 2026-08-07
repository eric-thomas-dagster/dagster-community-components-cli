# Wine ML Pipeline demo — py-minimal (3 assets, 1 component + custom Python)

> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

> 🧭 **Not sure which variant to pick?** See [`wine_ml.md`](wine_ml.md) — the shape-selector index for all wine variants with pros / cons and a decision tree.

**Cross of two axes**: **3 assets** (only real deliverables tracked, like `_minimal`) × **community components where they save work** (like `_py`). One `FileIngestionComponent` for the ingest asset (URL fetch + CSV parse + metadata is boilerplate worth delegating), custom Python for the model + CV stages (bespoke enough that a component would add friction). This is the **most common middle-ground shape** in real projects.

## When to reach for this over `_minimal` or `_py`

- **vs. [`_minimal`](wine_ml_pipeline_minimal.md)** (3 assets, no components): use py-minimal when at least one stage (usually ingest) has boilerplate worth delegating. `FileIngestionComponent` handles URL fetch + CSV parse + preview metadata + row-count metadata in one config line vs. ~15 lines of `requests` + `pd.read_csv` + `context.add_output_metadata`.
- **vs. [`_py`](wine_ml_pipeline_py.md)** (9 assets, all components): use py-minimal when the ML stages (scaling, splitting, training, CV) are custom enough that the components would fight you, OR when you don't need to track scaled / split as first-class assets.

## Pipeline

```
wine_raw (FileIngestionComponent) ──▶ wine_model_outputs (custom @multi_asset)
                              │            (scale + split + train + predict + importance + CSV writes)
                              │            → wine_predictions
                              │            → wine_feature_importance
                              │
                              └──▶ wine_cv_scores (custom @asset)
                                       → wine_cv.csv
```

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_wine_ml_pipeline_py_minimal_demo.sh | bash
cd wine-ml-pipeline-py-minimal-demo
uv run dg dev            # → http://localhost:3000 — click "Materialize all"
```

## The code

Write to `src/<pkg>/defs/pipeline.py`:

```python
"""Wine ML pipeline — py-minimal (3 assets, 1 component + custom Python).

FileIngestionComponent handles the URL-fetch + CSV-parse + metadata
boilerplate. Everything else is custom Python because the ML stages are
bespoke enough that per-project code is clearer than component config.
"""
import dagster as dg
import pandas as pd
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import train_test_split, cross_validate
from sklearn.tree import DecisionTreeClassifier

from dagster_community_components import FileIngestionComponent

FEATURES = [
    "fixed acidity", "volatile acidity", "citric acid", "residual sugar",
    "chlorides", "free sulfur dioxide", "total sulfur dioxide", "density",
    "pH", "sulphates", "alcohol",
]


# ── Plain Python helpers — shared between the two custom assets ───────

def _scale_features(df: pd.DataFrame) -> pd.DataFrame:
    scaler = StandardScaler()
    out = df.copy()
    out[FEATURES] = scaler.fit_transform(out[FEATURES])
    return out


def _train_predict_importance(scaled: pd.DataFrame):
    train, _ = train_test_split(
        scaled, test_size=0.2, stratify=scaled["quality"], random_state=42,
    )
    clf = DecisionTreeClassifier(max_depth=6, random_state=42)
    clf.fit(train[FEATURES], train["quality"])

    preds = scaled.copy()
    preds["predicted"] = clf.predict(scaled[FEATURES])

    importance = pd.DataFrame({
        "feature": FEATURES,
        "importance": clf.feature_importances_,
    }).sort_values("importance", ascending=False)

    return preds, importance


def _cross_validate(scaled: pd.DataFrame) -> pd.DataFrame:
    clf = DecisionTreeClassifier(max_depth=6, random_state=42)
    scores = cross_validate(
        clf, scaled[FEATURES], scaled["quality"],
        cv=5, return_train_score=True,
    )
    return pd.DataFrame({
        "fold": range(1, 6),
        "train_score": scores["train_score"],
        "test_score": scores["test_score"],
        "fit_time": scores["fit_time"],
    })


# ── ASSET 1 (via component): the ingested dataset ─────────────────────
#
# FileIngestionComponent handles requests.get + pd.read_csv + metadata
# preview + row count in one config. Zero per-project glue.

_ingest = FileIngestionComponent(
    asset_name="wine_raw",
    file_path="https://archive.ics.uci.edu/ml/machine-learning-databases/wine-quality/winequality-red.csv",
    description="UCI red wine quality dataset (1599 rows, 11 chemistry features).",
    delimiter=";",
    include_preview_metadata=True,
    group_name="ingest",
)


# ── ASSET 2 (custom): model deliverables — predictions + importance ───

@dg.multi_asset(
    outs={
        "wine_predictions": dg.AssetOut(group_name="model"),
        "wine_feature_importance": dg.AssetOut(group_name="model"),
    },
)
def wine_model_outputs(context: dg.AssetExecutionContext, wine_raw: pd.DataFrame):
    scaled = _scale_features(wine_raw)
    preds, importance = _train_predict_importance(scaled)
    preds.to_csv("/tmp/wine_predictions.csv", index=False)
    importance.to_csv("/tmp/wine_importance.csv", index=False)
    context.log.info(f"wrote 2 CSVs; {len(preds)} predictions, {len(importance)} features")
    return preds, importance


# ── ASSET 3 (custom): cross-validation scores ─────────────────────────

@dg.asset(group_name="validation")
def wine_cv_scores(context: dg.AssetExecutionContext, wine_raw: pd.DataFrame) -> pd.DataFrame:
    scaled = _scale_features(wine_raw)
    df = _cross_validate(scaled)
    df.to_csv("/tmp/wine_cv.csv", index=False)
    context.log.info(f"5-fold CV — mean test={df['test_score'].mean():.3f}")
    return df


# Merge the component's Definitions with the custom-code Definitions.
defs = dg.Definitions.merge(
    _ingest.build_defs(None),
    dg.Definitions(assets=[wine_model_outputs, wine_cv_scores]),
)
```

## Why this shape wins for many teams

The pain in real projects isn't "should we use components everywhere or nowhere" — it's "which stages have boilerplate worth delegating." **Ingestion is usually the answer**: URL fetch + CSV parse + delimiter handling + metadata previews + row-count tracking is genuinely repeated across ~every ingestion asset. Off-the-shelf. Delegate to `FileIngestionComponent`.

**Model stages usually aren't.** Every project's model has bespoke feature engineering, target column, task type, hyperparameter choices, and metric reporting. A `DecisionTreeModelComponent` exists but customizing it (add `class_weight`, change split strategy, extract non-standard metadata) means either (a) hoping the component exposes the config knob, or (b) forking. Custom Python is often cleaner.

**py-minimal captures this.** Use components where they save real per-project boilerplate; write custom Python where the logic is genuinely custom.

## Common extensions

- Add a second ingest component for a *different* source: e.g. `WhiteWineIngest = FileIngestionComponent(...)` for the sibling white wine dataset, then merge both into `wine_raw_all`.
- Swap in `AzureBlobIngestComponent` / `SnowflakeQueryComponent` / etc. depending on where the raw data lives. The custom model + CV code stays unchanged.
- Add a `PandasProfilingReportComponent` after ingest for automatic data-quality profiling — free.

## See also

- [`wine_ml.md`](wine_ml.md) — the shape-selector index (2D grid: asset granularity × decomposition style).
- [`wine_ml_pipeline_minimal.md`](wine_ml_pipeline_minimal.md) — same 3-asset shape but with zero components (all custom Python).
- [`wine_ml_pipeline_py.md`](wine_ml_pipeline_py.md) — same components-in-Python style but with 9 assets (per-stage tracking).
- [Walkthrough index](README.md) — 270+ end-to-end demos across every component family.
