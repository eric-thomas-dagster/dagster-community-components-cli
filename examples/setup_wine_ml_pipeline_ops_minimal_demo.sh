#!/usr/bin/env bash
# Wine ML Pipeline demo — ops-minimal (3 assets + typed @op composition).
#
# Cross of _minimal's asset shape (3 first-class assets) and _ops's typed
# op decomposition (sub-steps are @dg.op with typed I/O). Ops compose
# cleanly across BOTH graph-backed assets — scale_op is reused inside
# wine_model_outputs AND wine_cv_scores.
#
# Assets (3 total):
#   wine_raw ─┬─▶ wine_model_outputs (graph_multi_asset)
#             │     scale_op → train_op ─┬─▶ predict_and_write_op → wine_predictions
#             │                          └─▶ importance_and_write_op → wine_feature_importance
#             │
#             └─▶ wine_cv_scores (graph_asset)
#                   scale_op (reused) → cv_and_write_op → wine_cv_scores
#
# See wine_ml_pipeline_ops_minimal.md for the full walkthrough.

set -euo pipefail

PROJECT_DIR="${1:-wine-ml-pipeline-ops-minimal-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"
mkdir -p out
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps (no dagster-community-components — pure Dagster + ops)"
uv add -q pandas scikit-learn requests
uv add -q 'yarl<1.24'
uv add --dev -q dagster-dg-cli dagster-webserver

echo ">>> Writing single-file pipeline to src/$PKG/defs/pipeline.py"
cat > "src/$PKG/defs/pipeline.py" <<'PYEOF'
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
    out.to_csv("out/wine_predictions.csv", index=False)
    return out


@dg.op
def importance_and_write_op(clf: DecisionTreeClassifier) -> pd.DataFrame:
    df = pd.DataFrame({
        "feature": FEATURES,
        "importance": clf.feature_importances_,
    }).sort_values("importance", ascending=False)
    df.to_csv("out/wine_importance.csv", index=False)
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
    df.to_csv("out/wine_cv.csv", index=False)
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


# ── ASSET 3: cross-validation (scale_op reused across assets) ─────────

@dg.graph_asset(group_name="validation")
def wine_cv_scores(wine_raw: pd.DataFrame) -> pd.DataFrame:
    scaled = scale_op(wine_raw)         # SAME op as in wine_model_outputs
    return cv_and_write_op(scaled)


defs = dg.Definitions(
    assets=[wine_raw, wine_model_outputs, wine_cv_scores],
)
PYEOF

echo ""
echo ">>> Setup complete."
echo ""
echo "Materialize (open UI):"
echo "    cd $PROJECT_DIR && uv run dg dev"
echo "        → http://localhost:3000 — click Materialize all"
echo ""
echo "Or headless:"
echo "    cd $PROJECT_DIR && uv run dg launch --assets '*'"
echo "    ls -la $PROJECT_ABS/out/wine_*.csv"
echo ""
echo "See examples/wine_ml_pipeline_ops_minimal.md for the full walkthrough."
