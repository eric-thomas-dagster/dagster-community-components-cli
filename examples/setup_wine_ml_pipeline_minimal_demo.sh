#!/usr/bin/env bash
# Wine ML Pipeline demo — minimal (2 assets, everything else Python).
#
# Same 6-step logic as the other wine variants, but only the real
# deliverables are Dagster assets. Everything else (scaling, splitting,
# training, CSV writes) is a plain Python function inside a single
# @multi_asset body. Closest shape to a Prefect flow.
#
# Assets (3 total):
#   wine_raw ──▶ wine_model_outputs {predictions, importance}  → 2 CSVs
#           │
#           └──▶ wine_cv_scores                                 → 1 CSV
#
# See wine_ml_pipeline_minimal.md for the full walkthrough + why this
# shape is often the right choice for pragmatic pipelines.

set -euo pipefail

PROJECT_DIR="${1:-wine-ml-pipeline-minimal-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"
mkdir -p out
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps (no dagster-community-components — pure Dagster)"
uv add -q pandas scikit-learn requests
uv add -q 'yarl<1.24'
uv add --dev -q dagster-dg-cli dagster-webserver

echo ">>> Writing single-file pipeline to src/$PKG/defs/pipeline.py"
cat > "src/$PKG/defs/pipeline.py" <<'PYEOF'
"""Wine ML pipeline — minimal (3 assets, everything else Python).

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
    """Side-effect CSV write. Not an asset — the CSV is a byproduct."""
    df.to_csv(path, index=False)


# ── ASSET 1: the ingested dataset ─────────────────────────────────────

@dg.asset(group_name="ingest")
def wine_raw(context: dg.AssetExecutionContext) -> pd.DataFrame:
    """UCI red wine quality dataset (1599 rows, 11 chemistry features)."""
    resp = requests.get(WINE_URL, timeout=30)
    resp.raise_for_status()
    df = pd.read_csv(StringIO(resp.text), sep=";")
    context.log.info(f"Fetched {len(df)} rows, {len(df.columns)} cols")
    return df


# ── ASSET 2: model deliverables — predictions + importance from ONE fit ──

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

    _write_csv(preds, "out/wine_predictions.csv")
    _write_csv(importance, "out/wine_importance.csv")
    context.log.info("wrote $PROJECT_ABS/out/wine_predictions.csv + $PROJECT_ABS/out/wine_importance.csv")

    return preds, importance


# ── ASSET 3: cross-validation scores (independent branch) ─────────────

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
    _write_csv(df, "out/wine_cv.csv")
    context.log.info(f"5-fold CV — mean test={df['test_score'].mean():.3f}; wrote $PROJECT_ABS/out/wine_cv.csv")
    return df


defs = dg.Definitions(
    assets=[wine_raw, wine_model_outputs, wine_cv_scores],
)
PYEOF

echo ""
echo ">>> Setup complete."
echo ""
echo "Materialize (open UI):"
echo "    cd $PROJECT_DIR && uv run dg dev"
echo "        → http://localhost:3000 — click Materialize all (only 3 nodes to see)"
echo ""
echo "Or headless:"
echo "    cd $PROJECT_DIR && uv run dg launch --assets '*'"
echo "    ls -la $PROJECT_ABS/out/wine_*.csv"
echo ""
echo "See examples/wine_ml_pipeline_minimal.md for the full walkthrough"
echo "+ comparison with the higher-asset-count variants."
