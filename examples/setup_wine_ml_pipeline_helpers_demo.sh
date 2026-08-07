#!/usr/bin/env bash
# Wine ML Pipeline demo — assets + plain Python helper functions.
#
# Same 6-step pipeline as setup_wine_ml_pipeline_raw_demo.sh, but the
# model stage decomposes into plain undecorated Python helper functions
# called from a single @multi_asset body. No @ops, no graph-backed
# asset — just idiomatic Python organization.
#
# Pipeline (8 assets, identical to _raw + _ops variants):
#   wine_raw → wine_scaled ─┬─→ wine_split → wine_model {predictions, importance}
#                           │                    │
#                           │                    ├─→ wine_predictions_csv
#                           │                    └─→ wine_importance_csv
#                           └─→ wine_cv_scores ─────→ wine_cv_csv
#
# See wine_ml_pipeline_helpers.md for the full walkthrough + comparison
# against _raw (inline logic) and _ops (typed @op story).

set -euo pipefail

PROJECT_DIR="${1:-wine-ml-pipeline-helpers-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"
mkdir -p out
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps (no dagster-community-components — pure Dagster)"
uv add -q pandas scikit-learn requests
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

echo ">>> Writing single-file pipeline to src/$PKG/defs/pipeline.py"
cat > "src/$PKG/defs/pipeline.py" <<'PYEOF'
"""Wine ML pipeline — assets + plain Python helpers.

Linear stages are @dg.asset. Model stage is one @dg.multi_asset whose
body calls three plain undecorated helper functions (_train_model,
_predict, _extract_importance) — no @op, no graph asset. Same shape as
_raw but the helpers are extracted for unit-testability.
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
#
# Unit-testable in isolation (`from pipeline import _train_model` → call
# with real data). Portable to any other codebase that isn't Dagster.

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


# ── Linear stages — plain @asset funcs ─────────────────────────────────

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


# ── Model — one @multi_asset composes the helpers ─────────────────────

@dg.multi_asset(
    outs={
        "wine_predictions": dg.AssetOut(group_name="model"),
        "wine_feature_importance": dg.AssetOut(group_name="model"),
    },
)
def wine_model(context: dg.AssetExecutionContext, wine_split: pd.DataFrame):
    train = wine_split[wine_split["split"] == "train"]
    clf = _train_model(train)                     # one shared fit
    context.log.info(f"trained — {len(train)} rows, max_depth=6")
    return _predict(clf, wine_split), _extract_importance(clf)


# ── Cross-validation — plain @asset (independent branch) ──────────────

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


# ── CSV sinks — plain @asset funcs with side-effect writes ────────────

@dg.asset(group_name="sink")
def wine_predictions_csv(context: dg.AssetExecutionContext, wine_predictions: pd.DataFrame) -> None:
    wine_predictions.to_csv("out/wine_predictions.csv", index=False)
    context.log.info(f"Wrote {len(wine_predictions)} rows to $PROJECT_ABS/out/wine_predictions.csv")


@dg.asset(group_name="sink")
def wine_importance_csv(context: dg.AssetExecutionContext, wine_feature_importance: pd.DataFrame) -> None:
    wine_feature_importance.to_csv("out/wine_importance.csv", index=False)
    context.log.info(f"Wrote {len(wine_feature_importance)} rows to $PROJECT_ABS/out/wine_importance.csv")


@dg.asset(group_name="sink")
def wine_cv_csv(context: dg.AssetExecutionContext, wine_cv_scores: pd.DataFrame) -> None:
    wine_cv_scores.to_csv("out/wine_cv.csv", index=False)
    context.log.info(f"Wrote {len(wine_cv_scores)} rows to $PROJECT_ABS/out/wine_cv.csv")


# ── Wire into one Definitions ──────────────────────────────────────────

defs = dg.Definitions(
    assets=[
        wine_raw,
        wine_scaled,
        wine_split,
        wine_model,
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
echo "    ls -la $PROJECT_ABS/out/wine_*.csv"
echo ""
echo "See examples/wine_ml_pipeline_helpers.md for the full walkthrough"
echo "+ comparison with the other four wine variants."
