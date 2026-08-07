# Wine ML Pipeline demo — ops-minimal (3 assets, typed `@op` composition)

> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

> 🧭 **Not sure which variant to pick?** See [`wine_ml.md`](wine_ml.md) — the shape-selector index for all wine variants with pros / cons and a decision tree.

**Cross of two axes**: **3 assets** (only real deliverables tracked, like `_minimal`) × **typed `@op` composition** (sub-steps get first-class Dagster typing, like `_ops`). This is `_minimal`'s asset-shape crossed with `_ops`' decomposition style — a fifth "shape."

## When to reach for this over `_minimal` or `_ops`

- **vs. [`_minimal`](wine_ml_pipeline_minimal.md)** (3 assets, plain Python helpers): use ops-minimal when the sub-steps need Dagster tracking (typed I/O catches shape mismatches at graph-build time, per-op retry policies, cross-asset op reuse).
- **vs. [`_ops`](wine_ml_pipeline_ops.md)** (8 assets, ops for model stage only): use ops-minimal when the intermediate stages (scaled, split) don't deserve to be first-class assets. Fewer entries in the catalog; ops still give you typed sub-step composition.

## Pipeline

```
wine_raw ──▶ wine_model_outputs (graph_multi_asset)
        │       │
        │       ├─▶ scale_op → train_op ─┬─▶ predict_and_write_op    → wine_predictions
        │       │                        └─▶ importance_and_write_op → wine_feature_importance
        │       │
        └──▶ wine_cv_scores (graph_asset)
                ├─▶ scale_op (reused!) → cv_and_write_op → wine_cv_scores
```

**Note the op reuse**: `scale_op` is called inside BOTH `wine_model_outputs` and `wine_cv_scores` graphs. That's the payoff of ops over plain helpers — same function, two graph contexts, no duplication.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_wine_ml_pipeline_ops_minimal_demo.sh | bash
cd wine-ml-pipeline-ops-minimal-demo
uv run dg dev            # → http://localhost:3000 — click "Materialize all"
```

## The code

Write to `src/<pkg>/defs/pipeline.py`:

```python
"""Wine ML pipeline — ops-minimal (3 assets, typed @op composition).

Combines _minimal's asset shape (3 first-class assets) with _ops's typed
op decomposition. Ops compose cleanly across both graph-backed assets —
scale_op is reused inside wine_model_outputs and wine_cv_scores.
"""
from io import StringIO

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


# ── @ops — typed reusable sub-steps ──────────────────────────────────

@dg.op
def scale_op(df: pd.DataFrame) -> pd.DataFrame:
    """Reused across wine_model_outputs + wine_cv_scores. One op, two callers."""
    scaler = StandardScaler()
    out = df.copy()
    out[FEATURES] = scaler.fit_transform(out[FEATURES])
    return out


@dg.op
def train_op(scaled: pd.DataFrame) -> DecisionTreeClassifier:
    train, _ = train_test_split(
        scaled, test_size=0.2, stratify=scaled["quality"], random_state=42,
    )
    clf = DecisionTreeClassifier(max_depth=6, random_state=42)
    clf.fit(train[FEATURES], train["quality"])
    return clf


@dg.op
def predict_and_write_op(clf: DecisionTreeClassifier, scaled: pd.DataFrame) -> pd.DataFrame:
    out = scaled.copy()
    out["predicted"] = clf.predict(scaled[FEATURES])
    out.to_csv("/tmp/wine_predictions.csv", index=False)
    return out


@dg.op
def importance_and_write_op(clf: DecisionTreeClassifier) -> pd.DataFrame:
    df = pd.DataFrame({
        "feature": FEATURES,
        "importance": clf.feature_importances_,
    }).sort_values("importance", ascending=False)
    df.to_csv("/tmp/wine_importance.csv", index=False)
    return df


@dg.op
def cv_and_write_op(scaled: pd.DataFrame) -> pd.DataFrame:
    clf = DecisionTreeClassifier(max_depth=6, random_state=42)
    scores = cross_validate(
        clf, scaled[FEATURES], scaled["quality"],
        cv=5, return_train_score=True,
    )
    df = pd.DataFrame({
        "fold": range(1, 6),
        "train_score": scores["train_score"],
        "test_score": scores["test_score"],
        "fit_time": scores["fit_time"],
    })
    df.to_csv("/tmp/wine_cv.csv", index=False)
    return df


# ── ASSET 1: ingested dataset ──────────────────────────────────────────

@dg.asset(group_name="ingest")
def wine_raw(context: dg.AssetExecutionContext) -> pd.DataFrame:
    resp = requests.get(WINE_URL, timeout=30)
    resp.raise_for_status()
    df = pd.read_csv(StringIO(resp.text), sep=";")
    context.log.info(f"Fetched {len(df)} rows, {len(df.columns)} cols")
    return df


# ── ASSET 2: model deliverables — predictions + importance from ONE fit ──

@dg.graph_multi_asset(
    outs={
        "wine_predictions": dg.AssetOut(group_name="model"),
        "wine_feature_importance": dg.AssetOut(group_name="model"),
    },
)
def wine_model_outputs(wine_raw: pd.DataFrame):
    scaled = scale_op(wine_raw)
    clf = train_op(scaled)
    return predict_and_write_op(clf, scaled), importance_and_write_op(clf)


# ── ASSET 3: cross-validation scores (op reuse — scale_op again) ──────

@dg.graph_asset(group_name="validation")
def wine_cv_scores(wine_raw: pd.DataFrame) -> pd.DataFrame:
    scaled = scale_op(wine_raw)         # ← SAME op as in wine_model_outputs
    return cv_and_write_op(scaled)


defs = dg.Definitions(
    assets=[wine_raw, wine_model_outputs, wine_cv_scores],
)
```

## What you get vs `_minimal`

- **Typed sub-step composition**: `predict_and_write_op(clf, scaled)` will error at graph-build time if `clf` isn't a `DecisionTreeClassifier` or `scaled` isn't a DataFrame. Plain Python helpers only catch this at runtime.
- **Op reuse across assets**: `scale_op` lives in one place and both `wine_model_outputs` and `wine_cv_scores` call it. In `_minimal`, `_scale_features()` is a plain function so reuse works — but the discipline / patterns Dagster provides for op composition (retry policies, resource injection, per-op tags) don't apply.
- **Per-op retry policies** (via `@dg.op(retry_policy=...)`): retry `train_op` on OOM, don't retry `write_op` because that would double-write. Plain helpers make this awkward.
- **Cross-asset unit testing** via Dagster's op-execution APIs (`build_op_context` + direct call) — the plain-helpers version tests via direct call, which is simpler for the `_minimal` case.

## What you give up vs `_ops`

- **No intermediate assets**: can't materialize just `wine_scaled` from the UI to inspect. It's an op output, not a tracked artifact.
- **No per-stage metadata**: `wine_scaled`'s row count, dtypes, freshness — none of that lives in the catalog. Compute logs are the only place.

## See also

- [`wine_ml.md`](wine_ml.md) — the shape-selector index (2D grid: asset granularity × decomposition style).
- [`wine_ml_pipeline_minimal.md`](wine_ml_pipeline_minimal.md) — same 3-asset shape but with plain Python helpers instead of `@op`s.
- [`wine_ml_pipeline_ops.md`](wine_ml_pipeline_ops.md) — same `@op`+`@graph_multi_asset` shape but with 8 assets (per-stage tracking).
- [Walkthrough index](README.md) — 270+ end-to-end demos across every component family.
