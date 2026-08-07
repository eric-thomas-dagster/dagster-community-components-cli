# Wine ML Pipeline demo — minimal (2 assets, everything else Python)

> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

> 🧭 **Not sure which variant to pick?** See [`wine_ml.md`](wine_ml.md) — the shape-selector index for all six wine variants with pros / cons and a decision tree.

**Sixth variant of the wine pipeline** — same 6-step logic as the others, but only the *actual deliverables* are Dagster assets. Everything else (scaling, splitting, training, cross-validation, CSV writing) is a plain Python function called from inside a single `@dg.multi_asset` body. This is the closest shape to a Prefect flow: one entry point, many function calls, few first-class artifacts.

## Why this shape

The other four Python variants (`_raw`, `_helpers`, `_ops`, `_py`) all treat every pipeline stage as a separate asset — 8 assets total. That's a stretch for a wine demo. In production most teams don't actually want:

- `wine_scaled` — a transient StandardScaler output nobody queries directly
- `wine_split` — a transient train/test partition
- `wine_predictions_csv` / `wine_importance_csv` / `wine_cv_csv` — CSVs are side effects of writing the *real* asset

The **real assets** are:
1. `wine_raw` — the ingested dataset (someone might want to inspect / redownload / re-checksum)
2. `wine_model_outputs` — the model deliverables (predictions + feature importance from the same fit)
3. `wine_cv_scores` — validation metrics (audit trail for the model's stability)

Everything else is implementation detail. This variant makes that choice explicit.

## The six variants — pick your shape

| Variant | # of assets | Wiring | Best when |
|---|---:|---|---|
| [`wine_ml_pipeline_minimal.md`](wine_ml_pipeline_minimal.md) *(this one)* | 3 | 1 ingest asset + 1 multi_asset that does everything else | Most Prefect-like. Fewest first-class artifacts; everything else is plain Python inside |
| [`wine_ml_pipeline_raw.md`](wine_ml_pipeline_raw.md) | 8 | Raw `@dg.asset` funcs, inline pandas/sklearn | Every stage is a first-class asset; simplest per-stage code |
| [`wine_ml_pipeline_helpers.md`](wine_ml_pipeline_helpers.md) | 8 | Same 8 assets, but the model stage calls plain Python helpers | Same asset graph as raw; model helpers unit-testable in isolation |
| [`wine_ml_pipeline_ops.md`](wine_ml_pipeline_ops.md) | 8 | `@op` + `@graph_multi_asset` for the model | Sub-steps deserve typed I/O, per-op retry policies, cross-asset reuse |
| [`wine_ml_pipeline_py.md`](wine_ml_pipeline_py.md) | 9 | Community components, Python instantiation | Delegate pandas/sklearn boilerplate to tested components |
| [`wine_ml_pipeline.md`](wine_ml_pipeline.md) | 9 | Community components, YAML `defs.yaml` | Declarative — analysts / SREs edit config, not code |

**Same underlying computation in every variant.** The choice is about how many "things" you want Dagster to track as first-class assets.

## Pipeline

```
wine_raw ──▶ wine_model_outputs {predictions, feature_importance}   → 2 CSVs (side effects)
        │
        └──▶ wine_cv_scores                                          → 1 CSV (side effect)

Inside wine_model_outputs (all plain Python functions):
    scale_features → split_train_test → train_model → predict + extract_importance → write CSVs

Inside wine_cv_scores:
    scale_features → cross_validate → write CSV
```

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_wine_ml_pipeline_minimal_demo.sh | bash
cd wine-ml-pipeline-minimal-demo
uv run dg dev            # → http://localhost:3000 — click "Materialize all"
```

## Step-by-step

### 1. Scaffold + deps

```bash
uvx create-dagster@latest project wine-ml-pipeline-minimal-demo --no-uv-sync
cd wine-ml-pipeline-minimal-demo
uv add pandas scikit-learn requests
uv add --dev dagster-dg-cli dagster-webserver
```

### 2. Drop in the single-file pipeline

Write to `src/<pkg>/defs/pipeline.py`:

```python
"""Wine ML pipeline — minimal (2 assets, everything else Python).

Every stage that isn't a real deliverable is a plain Python function.
Closest shape to a Prefect flow — one entry point, many function calls,
few first-class artifacts.
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


# ── Plain Python helpers — no Dagster decorators ──────────────────────

def _scale_features(df: pd.DataFrame) -> pd.DataFrame:
    """Standardize the 11 chemistry features. Not an asset — a helper."""
    scaler = StandardScaler()
    out = df.copy()
    out[FEATURES] = scaler.fit_transform(out[FEATURES])
    return out


def _split_train_test(df: pd.DataFrame) -> pd.DataFrame:
    """80/20 stratified split with a `split` column. Not an asset — a helper."""
    train, test = train_test_split(
        df, test_size=0.2, stratify=df["quality"], random_state=42,
    )
    return pd.concat(
        [train.assign(split="train"), test.assign(split="test")],
        ignore_index=True,
    )


def _train_model(train_df: pd.DataFrame) -> DecisionTreeClassifier:
    """Fit a decision tree. Not an asset — a helper."""
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


def _write_csv(df: pd.DataFrame, path: str) -> None:
    """Side-effect CSV write. Not an asset — the CSV is a byproduct of the
    materialization, not a tracked artifact."""
    df.to_csv(path, index=False)


# ── ASSET 1: the ingested dataset (someone might inspect/redownload) ──

@dg.asset(group_name="ingest")
def wine_raw(context: dg.AssetExecutionContext) -> pd.DataFrame:
    """UCI red wine quality dataset (1599 rows, 11 chemistry features)."""
    resp = requests.get(WINE_URL, timeout=30)
    resp.raise_for_status()
    df = pd.read_csv(StringIO(resp.text), sep=";")
    context.log.info(f"Fetched {len(df)} rows, {len(df.columns)} cols")
    return df


# ── ASSET 2: the model deliverables — predictions + importance from ONE fit ──

@dg.multi_asset(
    outs={
        "wine_predictions": dg.AssetOut(group_name="model"),
        "wine_feature_importance": dg.AssetOut(group_name="model"),
    },
)
def wine_model_outputs(context: dg.AssetExecutionContext, wine_raw: pd.DataFrame):
    """Full model pipeline: scale → split → train → predict + importance → write CSVs."""
    scaled = _scale_features(wine_raw)
    split_df = _split_train_test(scaled)
    train = split_df[split_df["split"] == "train"]

    clf = _train_model(train)
    context.log.info(f"trained decision tree — {len(train)} train rows, max_depth=6")

    preds = _predict(clf, split_df)
    importance = _extract_importance(clf)

    # Side-effect writes — CSVs are byproducts, not first-class assets.
    _write_csv(preds, "/tmp/wine_predictions.csv")
    _write_csv(importance, "/tmp/wine_importance.csv")
    context.log.info("wrote /tmp/wine_predictions.csv + /tmp/wine_importance.csv")

    return preds, importance


# ── ASSET 3: cross-validation scores (audit trail — an actual deliverable) ──

@dg.asset(group_name="validation")
def wine_cv_scores(context: dg.AssetExecutionContext, wine_raw: pd.DataFrame) -> pd.DataFrame:
    """5-fold CV metrics. Independent of the model_outputs branch."""
    scaled = _scale_features(wine_raw)
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
    _write_csv(df, "/tmp/wine_cv.csv")
    context.log.info(f"5-fold CV — mean test={df['test_score'].mean():.3f}; wrote /tmp/wine_cv.csv")
    return df


defs = dg.Definitions(
    assets=[wine_raw, wine_model_outputs, wine_cv_scores],
)
```

### 3. Run

```bash
uv run dg dev
```

In the UI: **3 nodes** (wine_raw, wine_model_outputs which shows two output branches, wine_cv_scores). Click **Materialize all** — same three CSVs land in `/tmp/` as every other variant. The internal plain-Python steps show up in the compute logs, not in the asset graph.

## Trade-offs vs. the higher-asset-count variants

**What you gain:**
- Fewer things to name / group / partition / freshness-policy / observe
- Runs faster — no per-stage IO manager serialization + deserialization
- More Prefect-like reading order (one big flow with many function calls)

**What you lose:**
- Can't materialize just `wine_scaled` from the UI (it's not an asset anymore)
- Compute logs are the only place to see intermediate step timings — no per-asset metadata on scaled or split
- If the ingest → scaled step is expensive and you want to iterate on downstream logic quickly, you re-scale every run (the scaled DataFrame isn't cached in an IO manager)

## When to choose which

- **Minimal (this variant)**: prototyping, demos, teams that don't want every stage tracked.
- **8-asset variants (`_raw` / `_helpers` / `_ops`)**: production pipelines where each stage's health / freshness / lineage matters.
- **Components variants (`_py` / YAML)**: teams that want to delegate the pandas/sklearn glue to tested reusable components.

## See also

- [`wine_ml_pipeline_raw.md`](wine_ml_pipeline_raw.md) — 8 assets, everything at asset-grain (the "opposite end" of the design space from this variant).
- [`wine_ml_pipeline_helpers.md`](wine_ml_pipeline_helpers.md) — 8 assets but the model stage uses plain-Python helpers (halfway house).
- [`wine_ml_pipeline_ops.md`](wine_ml_pipeline_ops.md) — 8 assets with `@op` + `@graph_multi_asset` for the model stage.
- [`wine_ml_pipeline_py.md`](wine_ml_pipeline_py.md) — same pipeline via community components (Python).
- [`wine_ml_pipeline.md`](wine_ml_pipeline.md) — same pipeline via community components (YAML).
- [Walkthrough index](README.md) — 270+ end-to-end demos across every component family.
