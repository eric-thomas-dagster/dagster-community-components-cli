#!/usr/bin/env bash
# Wine ML Pipeline demo — pure Dagster (no community components).
#
# Same 6-step pipeline as setup_wine_ml_pipeline_demo.sh and
# setup_wine_ml_pipeline_py_demo.sh, but built with raw @dg.asset
# decorators and inline pandas/scikit-learn — no community components.
# This is the most Prefect-familiar shape.
#
# Pipeline (8 raw assets, all in src/<pkg>/defs/pipeline.py):
#   wine_raw → wine_scaled ─┬─→ wine_split → wine_model ─┬─→ wine_predictions_csv
#                           │                            └─→ wine_importance_csv
#                           └─→ wine_cv_scores ────────────→ wine_cv_csv
#
# See wine_ml_pipeline_raw.md for the full walkthrough + "when to use
# raw vs components" comparison.

set -euo pipefail

PROJECT_DIR="${1:-wine-ml-pipeline-raw-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps (no dagster-community-components — pure Dagster)"
uv add -q pandas scikit-learn requests
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

echo ">>> Writing single-file pipeline to src/$PKG/defs/pipeline.py"
cat > "src/$PKG/defs/pipeline.py" <<'PYEOF'
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
        wine_model,           # multi-asset — produces both predictions + importance
        wine_cv_scores,
        wine_predictions_csv,
        wine_importance_csv,
        wine_cv_csv,
    ],
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
echo "    ls -la /tmp/wine_*.csv"
echo ""
echo "See examples/wine_ml_pipeline_raw.md for the full walkthrough + comparison"
echo "with the components-based variants (wine_ml_pipeline_py.md / wine_ml_pipeline.md)."
