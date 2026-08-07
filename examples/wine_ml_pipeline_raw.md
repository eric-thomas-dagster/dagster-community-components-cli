# Wine ML Pipeline demo — pure Dagster (no components)

> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

> 🧭 **Not sure which variant to pick?** See [`wine_ml.md`](wine_ml.md) — the shape-selector index for all six wine variants with pros / cons and a decision tree.

**Same pipeline as [`wine_ml_pipeline.md`](wine_ml_pipeline.md) and [`wine_ml_pipeline_py.md`](wine_ml_pipeline_py.md), zero community components** — every step is a raw `@dg.asset`-decorated function with inline pandas/scikit-learn code. This is the most familiar shape for teams coming from Prefect (`@flow` + `@task`) or Airflow's TaskFlow API.

## The five variants — pick your shape

Same ML pipeline, five shapes:

| Variant | Wiring | Best when |
|---|---|---|
| [`wine_ml_pipeline_raw.md`](wine_ml_pipeline_raw.md) *(this one)* | Raw `@dg.asset` funcs, inline pandas/sklearn | Simplest; everything at asset grain — most Prefect-familiar |
| [`wine_ml_pipeline_helpers.md`](wine_ml_pipeline_helpers.md) | Same shape but the multi-asset body calls plain Python helper funcs | Same Dagster surface; helpers are unit-testable without touching Dagster |
| [`wine_ml_pipeline_ops.md`](wine_ml_pipeline_ops.md) | `@op` + `@graph_multi_asset` — sub-steps have first-class typed I/O | Sub-steps deserve Dagster tracking (typed I/O, per-op retries, cross-asset reuse) |
| [`wine_ml_pipeline_py.md`](wine_ml_pipeline_py.md) | Community components, Python instantiation | Delegate pandas/sklearn boilerplate to tested components |
| [`wine_ml_pipeline.md`](wine_ml_pipeline.md) | Community components, YAML `defs.yaml` | Declarative — analysts / SREs edit config, not code |

**All five run the same graph and produce byte-identical CSVs.** The choice is purely about the shape your team prefers to maintain.

## Pipeline

```
wine_raw ──▶ wine_scaled ──┬──▶ wine_split ──▶ wine_model ──┬──▶ wine_predictions_csv
                           │                                └──▶ wine_importance_csv
                           │
                           └──▶ wine_cv_scores ─────────────────▶ wine_cv_csv
```

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_wine_ml_pipeline_raw_demo.sh | bash
cd wine-ml-pipeline-raw-demo
uv run dg dev            # → http://localhost:3000 — click "Materialize all"
```

## Step-by-step

### 1. Scaffold + deps

```bash
uvx create-dagster@latest project wine-ml-pipeline-raw-demo --no-uv-sync
cd wine-ml-pipeline-raw-demo
uv add pandas scikit-learn requests
uv add --dev dagster-dg-cli dagster-webserver
```

**No `dagster-community-components` dep** — this variant uses only `dagster` + `pandas` + `scikit-learn`.

### 2. Drop in the single-file pipeline

Write to `src/<pkg>/defs/pipeline.py`:

```python
"""Wine ML pipeline — pure Dagster, no components.

Every asset is a raw @dg.asset function with inline pandas/scikit-learn.
Compare to wine_ml_pipeline_py.md (same pipeline via community components).
"""
from io import StringIO

import dagster as dg
import pandas as pd
import requests
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import train_test_split, cross_validate
from sklearn.tree import DecisionTreeClassifier

# 11 chemistry features. Shared across scaler, model, and CV.
FEATURES = [
    "fixed acidity", "volatile acidity", "citric acid", "residual sugar",
    "chlorides", "free sulfur dioxide", "total sulfur dioxide", "density",
    "pH", "sulphates", "alcohol",
]

WINE_URL = "https://archive.ics.uci.edu/ml/machine-learning-databases/wine-quality/winequality-red.csv"


# ── 1. Ingest ──────────────────────────────────────────────────────────
@dg.asset(group_name="ingest")
def wine_raw(context: dg.AssetExecutionContext) -> pd.DataFrame:
    """UCI red wine quality dataset (1599 rows, 11 chemistry features, quality 3-8)."""
    resp = requests.get(WINE_URL, timeout=30)
    resp.raise_for_status()
    df = pd.read_csv(StringIO(resp.text), sep=";")
    context.log.info(f"Fetched {len(df)} rows, {len(df.columns)} cols")
    context.add_output_metadata({
        "row_count": dg.MetadataValue.int(len(df)),
        "columns": dg.MetadataValue.json(list(df.columns)),
        "preview": dg.MetadataValue.md(df.head(10).to_markdown(index=False)),
    })
    return df


# ── 2. Standardize features ────────────────────────────────────────────
@dg.asset(group_name="transform")
def wine_scaled(context: dg.AssetExecutionContext, wine_raw: pd.DataFrame) -> pd.DataFrame:
    """Standardize the 11 chemistry features to zero mean / unit variance."""
    scaler = StandardScaler()
    df = wine_raw.copy()
    df[FEATURES] = scaler.fit_transform(df[FEATURES])
    context.log.info(f"Scaled {len(FEATURES)} features")
    return df


# ── 3. Train/test split ────────────────────────────────────────────────
@dg.asset(group_name="transform")
def wine_split(context: dg.AssetExecutionContext, wine_scaled: pd.DataFrame) -> pd.DataFrame:
    """80/20 train/test split, stratified on quality. Adds a `split` column."""
    train, test = train_test_split(
        wine_scaled, test_size=0.2, stratify=wine_scaled["quality"], random_state=42,
    )
    train = train.assign(split="train")
    test = test.assign(split="test")
    df = pd.concat([train, test], ignore_index=True)
    context.log.info(f"Split into {len(train)} train + {len(test)} test")
    return df


# ── 4 + 5. Model — two output branches ─────────────────────────────────
@dg.multi_asset(
    outs={
        "wine_predictions": dg.AssetOut(group_name="model"),
        "wine_feature_importance": dg.AssetOut(group_name="model"),
    },
)
def wine_model(context: dg.AssetExecutionContext, wine_split: pd.DataFrame):
    """Fit a decision tree; emit two outputs — per-row predictions +
    per-feature importance."""
    train = wine_split[wine_split["split"] == "train"]
    test = wine_split[wine_split["split"] == "test"]

    clf = DecisionTreeClassifier(max_depth=6, random_state=42)
    clf.fit(train[FEATURES], train["quality"])

    # Branch 1 — predictions on the whole (train + test) dataset.
    preds = wine_split.copy()
    preds["predicted"] = clf.predict(wine_split[FEATURES])

    # Branch 2 — per-feature importance ranking.
    importance = pd.DataFrame({
        "feature": FEATURES,
        "importance": clf.feature_importances_,
    }).sort_values("importance", ascending=False)

    train_acc = clf.score(train[FEATURES], train["quality"])
    test_acc = clf.score(test[FEATURES], test["quality"])
    context.log.info(f"Trained decision tree — train acc={train_acc:.3f}, test acc={test_acc:.3f}")

    return preds, importance


# ── 6. Cross-validation ────────────────────────────────────────────────
@dg.asset(group_name="validation")
def wine_cv_scores(context: dg.AssetExecutionContext, wine_scaled: pd.DataFrame) -> pd.DataFrame:
    """5-fold cross-validation. Emits per-fold train/test score + fit time."""
    clf = DecisionTreeClassifier(max_depth=6, random_state=42)
    scores = cross_validate(
        clf, wine_scaled[FEATURES], wine_scaled["quality"],
        cv=5, return_train_score=True, return_estimator=False,
    )
    df = pd.DataFrame({
        "fold": range(1, 6),
        "train_score": scores["train_score"],
        "test_score": scores["test_score"],
        "fit_time": scores["fit_time"],
    })
    context.log.info(f"5-fold CV — mean test={df['test_score'].mean():.3f}")
    return df


# ── 7-9. CSV sinks ─────────────────────────────────────────────────────
@dg.asset(group_name="sink")
def wine_predictions_csv(context: dg.AssetExecutionContext, wine_predictions: pd.DataFrame) -> None:
    path = "/tmp/wine_predictions.csv"
    wine_predictions.to_csv(path, index=False)
    context.log.info(f"Wrote {len(wine_predictions)} rows to {path}")


@dg.asset(group_name="sink")
def wine_importance_csv(context: dg.AssetExecutionContext, wine_feature_importance: pd.DataFrame) -> None:
    path = "/tmp/wine_importance.csv"
    wine_feature_importance.to_csv(path, index=False)
    context.log.info(f"Wrote {len(wine_feature_importance)} rows to {path}")


@dg.asset(group_name="sink")
def wine_cv_csv(context: dg.AssetExecutionContext, wine_cv_scores: pd.DataFrame) -> None:
    path = "/tmp/wine_cv.csv"
    wine_cv_scores.to_csv(path, index=False)
    context.log.info(f"Wrote {len(wine_cv_scores)} rows to {path}")


# ── Wire everything into one Definitions ───────────────────────────────
defs = dg.Definitions(
    assets=[
        wine_raw,
        wine_scaled,
        wine_split,
        wine_model,           # multi-asset → produces both predictions + importance
        wine_cv_scores,
        wine_predictions_csv,
        wine_importance_csv,
        wine_cv_csv,
    ],
)
```

### 3. Run

```bash
uv run dg dev             # → http://localhost:3000 — click Materialize all
```

Or headless:

```bash
uv run dg launch --assets '*'
ls -la /tmp/wine_*.csv
```

## What you save by using components

Compare the same 6 pipeline steps across the three variants:

| Step | Raw (this file) | Components (Python) | Components (YAML) |
|---|---|---|---|
| Ingest CSV | 15 lines: requests + pd.read_csv + metadata | 6-line `FileIngestionComponent(...)` | 8-line defs.yaml |
| Feature scaling | 8 lines: sklearn StandardScaler | 6-line `FeatureScalerComponent(...)` | 15-line defs.yaml (column list) |
| Train/test split | 10 lines: sklearn train_test_split | 7-line `CreateSamplesComponent(...)` | 9-line defs.yaml |
| Decision tree × 2 branches | 25 lines: @multi_asset + fit + predict + importance | 2 × 9-line `DecisionTreeModelComponent(...)` | 2 × 22-line defs.yaml |
| Cross-validation | 15 lines: sklearn cross_validate + DataFrame | 8-line `CrossValidationComponent(...)` | 15-line defs.yaml |
| CSV sinks × 3 | 3 × 4-line @dg.asset | 3 × 5-line `DataframeToCsvComponent(...)` | 3 × 6-line defs.yaml |
| **Total** | ~180 lines | ~90 lines | ~140 lines YAML |

The **content** is identical — same fit params, same output columns, same file paths. The community components pre-package the pandas/sklearn glue so you don't rewrite it per-project. Each component is also tested end-to-end, so you don't reimplement the "empty upstream" / "missing column" / "IO manager dict-concat" edge cases.

**Trade-off**: components hide the implementation. When you need to customize (e.g. add class_weight to the DecisionTreeClassifier), you either (a) pass a matching field if the component exposes it, or (b) drop back to raw `@dg.asset` for that specific step. Mixing shapes is fine.

## See also

- [`wine_ml_pipeline_minimal.md`](wine_ml_pipeline_minimal.md) — same pipeline with only 3 assets (scaling/splitting/CSV writes are plain Python; most Prefect-like).
- [`wine_ml_pipeline_helpers.md`](wine_ml_pipeline_helpers.md) — same as raw, but the multi-asset body calls plain Python helper funcs.
- [`wine_ml_pipeline_ops.md`](wine_ml_pipeline_ops.md) — same shape with @op + @graph_multi_asset — first-class typed I/O per step.
- [`wine_ml_pipeline_py.md`](wine_ml_pipeline_py.md) — same pipeline via community components (Python instantiation).
- [`wine_ml_pipeline.md`](wine_ml_pipeline.md) — same pipeline via YAML defs.yaml files.
- [`titanic_complete.md`](titanic_complete.md) — larger ML pipeline (12 components) on the Titanic dataset.
- [`airports_cluster.md`](airports_cluster.md) — unsupervised ML variant (k-means clustering).
- [`wine_ml_pipeline_component.md`](wine_ml_pipeline_component.md) — same pipeline as ONE MLPipelineComponent (single YAML, standardized ML shape).
- [Walkthrough index](README.md) — 270+ end-to-end demos across every component family.
