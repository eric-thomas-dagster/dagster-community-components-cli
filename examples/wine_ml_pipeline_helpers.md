# Wine ML Pipeline demo — assets + plain Python helper functions

> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

> 🧭 **Not sure which variant to pick?** See [`wine_ml.md`](wine_ml.md) — the shape-selector index for all six wine variants with pros / cons and a decision tree.

**Fifth variant of the wine trilogy** — same 6-step pipeline as [`wine_ml_pipeline_raw.md`](wine_ml_pipeline_raw.md), but the model stage decomposes into **plain undecorated Python helper functions** called from a single `@multi_asset` body. No `@op`s, no graph-backed asset, no framework surface beyond `@dg.asset` / `@dg.multi_asset` — just idiomatic Python code organization.

## The five variants — pick your shape

| Variant | Wiring | Best when |
|---|---|---|
| [`wine_ml_pipeline_raw.md`](wine_ml_pipeline_raw.md) | Raw `@dg.asset` + `@dg.multi_asset`, everything inline in decorator bodies | Simplest; everything at asset grain |
| [`wine_ml_pipeline_helpers.md`](wine_ml_pipeline_helpers.md) *(this one)* | Same as raw, but the multi-asset body calls plain Python helper funcs | Same Dagster surface as raw, cleaner Python organization; each helper is unit-testable without touching Dagster |
| [`wine_ml_pipeline_ops.md`](wine_ml_pipeline_ops.md) | `@op`s composed by `@graph_multi_asset` | Sub-steps deserve first-class Dagster tracking (typed I/O, individual retry policies, cross-asset reuse) |
| [`wine_ml_pipeline_py.md`](wine_ml_pipeline_py.md) | Community components, Python instantiation | Delegate pandas/sklearn boilerplate to tested components |
| [`wine_ml_pipeline.md`](wine_ml_pipeline.md) | Community components, YAML `defs.yaml` | Declarative — analysts / SREs edit config, not code |

## What's different from `_raw` vs `_ops`

**vs. [`_raw`](wine_ml_pipeline_raw.md)**: `_raw` has the fit + predict + importance logic inline inside `wine_model`'s `@multi_asset` body — one long function. `_helpers` extracts those into three named private functions (`_train_model`, `_predict`, `_extract_importance`) — same behavior, cleaner reading order and unit-testable in isolation.

**vs. [`_ops`](wine_ml_pipeline_ops.md)**: `_ops` uses `@dg.op` on each helper + wraps them in `@graph_multi_asset`. `_helpers` is the same shape *without* the Dagster decorators — plain Python functions. The trade-off: `_ops` gets typed I/O, per-op retry policies, and cross-asset op reuse; `_helpers` gets zero-Dagster helpers that could move to any other codebase.

**Bottom line**: `_helpers` is the "least Dagster surface" way to keep helper functions clean. Use it when you don't need the op story — most teams don't.

## Pipeline

```
wine_raw ──▶ wine_scaled ──┬──▶ wine_split ──▶ wine_model ──┬──▶ wine_predictions_csv
                           │  (@multi_asset body calls        │
                           │   _train_model, _predict,        └──▶ wine_importance_csv
                           │   _extract_importance)
                           │
                           └──▶ wine_cv_scores ─────────────────▶ wine_cv_csv
```

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_wine_ml_pipeline_helpers_demo.sh | bash
cd wine-ml-pipeline-helpers-demo
uv run dg dev            # → http://localhost:3000 — click "Materialize all"
```

## Step-by-step

### 1. Scaffold + deps

```bash
uvx create-dagster@latest project wine-ml-pipeline-helpers-demo --no-uv-sync
cd wine-ml-pipeline-helpers-demo
uv add pandas scikit-learn requests
uv add --dev dagster-dg-cli dagster-webserver
```

**No `dagster-community-components`**, no ops framework — just `dagster` + `pandas` + `scikit-learn`.

### 2. Drop in the single-file pipeline

Write to `src/<pkg>/defs/pipeline.py`. The key section is the model stage:

```python
# ── Plain Python helper functions — no Dagster decorators ─────────────
# Testable in isolation (`from pipeline import _train_model` → call w/
# real data), portable to any other codebase.

def _train_model(train_df: pd.DataFrame) -> DecisionTreeClassifier:
    clf = DecisionTreeClassifier(max_depth=6, random_state=42)
    clf.fit(train_df[FEATURES], train_df["quality"])
    return clf

def _predict(clf: DecisionTreeClassifier, df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["predicted"] = clf.predict(df[FEATURES])
    return out

def _extract_importance(clf: DecisionTreeClassifier) -> pd.DataFrame:
    return pd.DataFrame({
        "feature": FEATURES,
        "importance": clf.feature_importances_,
    }).sort_values("importance", ascending=False)


# ── Single @multi_asset composes the helpers ──────────────────────────
@dg.multi_asset(
    outs={
        "wine_predictions": dg.AssetOut(group_name="model"),
        "wine_feature_importance": dg.AssetOut(group_name="model"),
    },
)
def wine_model(context: dg.AssetExecutionContext, wine_split: pd.DataFrame):
    train = wine_split[wine_split["split"] == "train"]
    clf = _train_model(train)                    # one shared fit
    context.log.info(f"trained — {len(train)} rows, max_depth=6")
    return _predict(clf, wine_split), _extract_importance(clf)
```

The linear stages (`wine_raw`, `wine_scaled`, `wine_split`, `wine_cv_scores`) and CSV sinks stay as plain `@dg.asset` funcs — identical to [`wine_ml_pipeline_raw.md`](wine_ml_pipeline_raw.md).

### 3. Run

```bash
uv run dg dev             # → http://localhost:3000 — click Materialize all
```

Or headless:

```bash
uv run dg launch --assets '*'
ls -la /tmp/wine_*.csv
```

## Why decompose with plain functions?

Two reasons the `_raw` style body gets rewritten this way as a project matures:

1. **Unit testing**: `_train_model(some_df)` can be called in a pytest without any Dagster imports or context stubs. `_raw`-style inline logic requires a full asset materialization to exercise.

2. **Reading order**: the `@multi_asset` body becomes a short recipe of named steps — reads top-to-bottom like a script. The pandas/sklearn plumbing lives above (or in another module).

## Testing the helpers

```python
# tests/test_helpers.py — pure Python, no Dagster
import pandas as pd
from pipeline import FEATURES, _train_model, _predict, _extract_importance

def _fake_wine(n=100):
    return pd.DataFrame({
        **{f: [0.5] * n for f in FEATURES},
        "quality": [5] * n,
    })

def test_predict_covers_input_rows():
    df = _fake_wine(50)
    clf = _train_model(df)
    out = _predict(clf, df)
    assert len(out) == len(df)
    assert "predicted" in out.columns

def test_importance_sums_to_one():
    clf = _train_model(_fake_wine())
    imp = _extract_importance(clf)
    assert abs(imp["importance"].sum() - 1.0) < 1e-9
```

Same tests aren't possible against `_raw`-style inline logic without either (a) refactoring first, or (b) full-materialization tests. That's the paying-off in the mature-project case.

## See also

- [`wine_ml_pipeline_minimal.md`](wine_ml_pipeline_minimal.md) — same pipeline with only 3 assets (scaling/splitting/CSV writes are plain Python; most Prefect-like).
- [`wine_ml_pipeline_raw.md`](wine_ml_pipeline_raw.md) — inline logic (this variant's starting point).
- [`wine_ml_pipeline_ops.md`](wine_ml_pipeline_ops.md) — same shape but helpers are `@dg.op` functions inside a `@dg.graph_multi_asset`.
- [`wine_ml_pipeline_py.md`](wine_ml_pipeline_py.md) — same pipeline via community components (Python instantiation).
- [`wine_ml_pipeline.md`](wine_ml_pipeline.md) — same pipeline via YAML `defs.yaml` files.
- [Walkthrough index](README.md) — 270+ end-to-end demos across every component family.
