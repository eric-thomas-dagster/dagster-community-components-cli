# Wine ML Pipeline demo — assets + ops (graph-backed asset)

> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

> 🧭 **Not sure which variant to pick?** See [`wine_ml.md`](wine_ml.md) — the shape-selector index for all six wine variants with pros / cons and a decision tree.

**Fourth variant of the wine trilogy** — same 6-step pipeline as [`wine_ml_pipeline_raw.md`](wine_ml_pipeline_raw.md), but the model stage is decomposed into **three `@op` functions composed into a `@graph_multi_asset`**. Every op is a plain decorated function — reads like Prefect `@task`s but plugs into Dagster's asset graph.

## The five variants — pick your shape

| Variant | Wiring | Best when |
|---|---|---|
| [`wine_ml_pipeline_raw.md`](wine_ml_pipeline_raw.md) | Raw `@dg.asset` funcs, inline pandas/sklearn | Simplest; everything at asset grain |
| [`wine_ml_pipeline_helpers.md`](wine_ml_pipeline_helpers.md) | Same shape but the multi-asset body calls plain Python helper funcs | Same Dagster surface; helpers are unit-testable without touching Dagster |
| [`wine_ml_pipeline_ops.md`](wine_ml_pipeline_ops.md) *(this one)* | `@op` + `@graph_multi_asset` — sub-steps have first-class typed I/O | Sub-steps deserve Dagster tracking (typed I/O, per-op retries, cross-asset reuse) |
| [`wine_ml_pipeline_py.md`](wine_ml_pipeline_py.md) | Community components, Python instantiation | Delegate pandas/sklearn boilerplate to tested components |
| [`wine_ml_pipeline.md`](wine_ml_pipeline.md) | Community components, YAML `defs.yaml` | Declarative — analysts / SREs edit config, not code |

## Why mix ops with assets?

An **asset** is a thing that gets tracked and materialized (a table, a file, an ML model). An **op** is a unit of work — a Python function with typed inputs and outputs. In pure-asset mode, one `@asset` = one op = one materialized thing. But sometimes a single asset's compute is naturally decomposed into several sub-steps that don't each deserve their own materialization tracking.

**Wine model example:**
- `train_model` — fits a decision tree on the train split
- `predict` — applies the trained model to the full dataset
- `feature_importance` — extracts per-feature importance from the trained model

These three steps share the trained model object; separating them into three `@asset`s would mean re-fitting the model (or fighting IO managers to serialize a sklearn object). Composing them into one `@graph_multi_asset` with three `@op`s gives you:

- Single fit, two output branches (`wine_predictions` + `wine_feature_importance`)
- Each sub-op is testable in isolation
- Both output branches remain first-class assets in the graph

## Pipeline

```
wine_raw ──▶ wine_scaled ──┬──▶ wine_split ──┬─▶ [ train_model ─▶ predict ] ─▶ wine_predictions
                           │                 │                                 │
                           │                 └─▶ [ train_model ─▶ importance ]─▶ wine_feature_importance
                           │                     (one shared fit — graph-backed multi-asset)
                           │
                           └──▶ wine_cv_scores ─────────────────────────────────▶ wine_cv_csv
```

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_wine_ml_pipeline_ops_demo.sh | bash
cd wine-ml-pipeline-ops-demo
uv run dg dev            # → http://localhost:3000 — click "Materialize all"
```

## Step-by-step

### 1. Scaffold + deps

```bash
uvx create-dagster@latest project wine-ml-pipeline-ops-demo --no-uv-sync
cd wine-ml-pipeline-ops-demo
uv add pandas scikit-learn requests
uv add --dev dagster-dg-cli dagster-webserver
```

**No `dagster-community-components`** — this variant is pure Dagster (like `_raw`).

### 2. Drop in the single-file pipeline

Write to `src/<pkg>/defs/pipeline.py`:

```python
"""Wine ML pipeline — assets + ops (graph-backed).

Linear pipeline stages are @dg.asset funcs. The model stage decomposes into
three @dg.op funcs (train_model → predict + feature_importance) composed
by @dg.graph_multi_asset so one shared fit powers both output branches.
"""
from io import StringIO
from typing import Tuple

import dagster as dg
import pandas as pd
import requests
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import train_test_split, cross_validate
from sklearn.tree import DecisionTreeClassifier

FEATURES = [
    "fixed acidity", "volatile acidity", "citric acid", "residual sugar",
    "chlorides", "free sulfur dioxide", "total sulfur dioxide", "density",
    "pH", "sulphates", "alcohol",
]

WINE_URL = "https://archive.ics.uci.edu/ml/machine-learning-databases/wine-quality/winequality-red.csv"


# ── Plain @asset funcs for the linear stages ───────────────────────────

@dg.asset(group_name="ingest")
def wine_raw(context: dg.AssetExecutionContext) -> pd.DataFrame:
    resp = requests.get(WINE_URL, timeout=30)
    resp.raise_for_status()
    df = pd.read_csv(StringIO(resp.text), sep=";")
    context.log.info(f"Fetched {len(df)} rows, {len(df.columns)} cols")
    return df


@dg.asset(group_name="transform")
def wine_scaled(context: dg.AssetExecutionContext, wine_raw: pd.DataFrame) -> pd.DataFrame:
    scaler = StandardScaler()
    df = wine_raw.copy()
    df[FEATURES] = scaler.fit_transform(df[FEATURES])
    return df


@dg.asset(group_name="transform")
def wine_split(context: dg.AssetExecutionContext, wine_scaled: pd.DataFrame) -> pd.DataFrame:
    train, test = train_test_split(
        wine_scaled, test_size=0.2, stratify=wine_scaled["quality"], random_state=42,
    )
    return pd.concat(
        [train.assign(split="train"), test.assign(split="test")],
        ignore_index=True,
    )


# ── @op funcs — decomposed model logic ────────────────────────────────
#
# Each @op is a plain decorated function. Ops are the Dagster analog of
# Prefect @task or Airflow's TaskFlow — a unit of work with typed I/O.
# Ops don't show up as assets in the Dagster UI; they're internal
# building blocks that compose into graph-backed assets below.

@dg.op
def train_model(wine_split: pd.DataFrame) -> DecisionTreeClassifier:
    """Fit a decision tree on the train split. Returns the fitted model."""
    train = wine_split[wine_split["split"] == "train"]
    clf = DecisionTreeClassifier(max_depth=6, random_state=42)
    clf.fit(train[FEATURES], train["quality"])
    return clf


@dg.op
def predict(clf: DecisionTreeClassifier, wine_split: pd.DataFrame) -> pd.DataFrame:
    """Apply the fitted model to the full (train + test) dataset."""
    preds = wine_split.copy()
    preds["predicted"] = clf.predict(wine_split[FEATURES])
    return preds


@dg.op
def feature_importance(clf: DecisionTreeClassifier) -> pd.DataFrame:
    """Extract per-feature importance from the fitted model."""
    return pd.DataFrame({
        "feature": FEATURES,
        "importance": clf.feature_importances_,
    }).sort_values("importance", ascending=False)


# ── @graph_multi_asset — composes the three ops above ──────────────────
#
# Emits TWO asset outputs from ONE shared fit: predictions + importance.
# Alternative would be two separate @asset funcs each doing their own fit
# (wasteful) or a @multi_asset with one big body (harder to unit-test).

@dg.graph_multi_asset(
    outs={
        "wine_predictions": dg.AssetOut(group_name="model"),
        "wine_feature_importance": dg.AssetOut(group_name="model"),
    },
)
def wine_model(wine_split: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    clf = train_model(wine_split)
    preds = predict(clf, wine_split)
    importance = feature_importance(clf)
    return preds, importance


# ── Cross-validation — plain @asset (independent branch) ───────────────

@dg.asset(group_name="validation")
def wine_cv_scores(context: dg.AssetExecutionContext, wine_scaled: pd.DataFrame) -> pd.DataFrame:
    clf = DecisionTreeClassifier(max_depth=6, random_state=42)
    scores = cross_validate(
        clf, wine_scaled[FEATURES], wine_scaled["quality"],
        cv=5, return_train_score=True,
    )
    return pd.DataFrame({
        "fold": range(1, 6),
        "train_score": scores["train_score"],
        "test_score": scores["test_score"],
        "fit_time": scores["fit_time"],
    })


# ── CSV sinks — plain @asset funcs with side-effect writes ─────────────

@dg.asset(group_name="sink")
def wine_predictions_csv(context: dg.AssetExecutionContext, wine_predictions: pd.DataFrame) -> None:
    wine_predictions.to_csv("/tmp/wine_predictions.csv", index=False)
    context.log.info(f"Wrote {len(wine_predictions)} rows to /tmp/wine_predictions.csv")


@dg.asset(group_name="sink")
def wine_importance_csv(context: dg.AssetExecutionContext, wine_feature_importance: pd.DataFrame) -> None:
    wine_feature_importance.to_csv("/tmp/wine_importance.csv", index=False)
    context.log.info(f"Wrote {len(wine_feature_importance)} rows to /tmp/wine_importance.csv")


@dg.asset(group_name="sink")
def wine_cv_csv(context: dg.AssetExecutionContext, wine_cv_scores: pd.DataFrame) -> None:
    wine_cv_scores.to_csv("/tmp/wine_cv.csv", index=False)
    context.log.info(f"Wrote {len(wine_cv_scores)} rows to /tmp/wine_cv.csv")


# ── Wire into one Definitions ──────────────────────────────────────────

defs = dg.Definitions(
    assets=[
        wine_raw,
        wine_scaled,
        wine_split,
        wine_model,           # graph-backed multi-asset — internally 3 ops
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

## What the UI shows

The asset graph shows **8 assets** — the three `@op` functions inside `wine_model` don't render as separate assets. They're internal building blocks. But when you click `wine_predictions` or `wine_feature_importance` in the UI, the "compute log" panel shows the three op steps (`train_model`, `predict`, `feature_importance`) executing in sequence.

The `wine_model` node in the graph shows as a **single multi-asset with two outputs** — same visual as `@multi_asset` in [`wine_ml_pipeline_raw.md`](wine_ml_pipeline_raw.md), but the *internal* implementation is now a graph of three testable ops.

## When to reach for `@graph_multi_asset` vs `@multi_asset`

| Pattern | Use when |
|---|---|
| `@dg.asset` | The stage is one function, no sub-steps worth tracking |
| `@dg.multi_asset` | The stage produces multiple outputs from one body, and you don't need to test the sub-steps separately |
| `@dg.graph_multi_asset` + `@dg.op` | The stage produces multiple outputs from a shared intermediate (like a fitted model), AND you want each sub-step testable in isolation |

Ops also compose across graph assets — you can reuse the same `train_model` op inside a hyperparameter-sweep graph or a walk-forward-validation graph without rewriting it. That's where the ops-first shape starts to pay off vs. the pure-asset shape.

## See also

- [`wine_ml_pipeline_minimal.md`](wine_ml_pipeline_minimal.md) — same pipeline with only 3 assets (scaling/splitting/CSV writes are plain Python; most Prefect-like).
- [`wine_ml_pipeline_raw.md`](wine_ml_pipeline_raw.md) — same pipeline, all `@asset` funcs, no ops.
- [`wine_ml_pipeline_py.md`](wine_ml_pipeline_py.md) — same pipeline via community components (Python instantiation).
- [`wine_ml_pipeline.md`](wine_ml_pipeline.md) — same pipeline via YAML `defs.yaml` files.
- [Dagster docs — passing data between assets](https://docs.dagster.io/guides/build/assets/passing-data-between-assets) — deeper on the IO manager story that connects asset outputs to downstream inputs.
- [`wine_ml_pipeline_ops_minimal.md`](wine_ml_pipeline_ops_minimal.md) — same `@op`+`@graph_multi_asset` style but with only 3 assets (compressed).
- [`wine_ml_pipeline_component.md`](wine_ml_pipeline_component.md) — same pipeline as ONE MLPipelineComponent (single YAML, standardized ML shape).
- [Walkthrough index](README.md) — 270+ end-to-end demos across every component family.
