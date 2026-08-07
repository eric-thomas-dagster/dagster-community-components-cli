#!/usr/bin/env bash
# Wine ML Pipeline demo — py-minimal (3 assets + 1 community component +
# custom Python for the rest).
#
# The most common middle-ground shape in real projects. Delegate the
# boilerplate stages (ingest) to a community component; write custom
# Python for the bespoke stages (model, CV) where component config would
# add friction.
#
# Assets (3 total):
#   wine_raw (FileIngestionComponent) ─┬─▶ wine_model_outputs (custom @multi_asset)
#                                       │     → wine_predictions
#                                       │     → wine_feature_importance
#                                       │
#                                       └─▶ wine_cv_scores (custom @asset)
#
# See wine_ml_pipeline_py_minimal.md for the full walkthrough.

set -euo pipefail

PROJECT_DIR="${1:-wine-ml-pipeline-py-minimal-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"
mkdir -p out
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas scikit-learn requests dagster-community-components
uv add -q 'yarl<1.24'
uv add --dev -q dagster-dg-cli dagster-webserver

echo ">>> Writing single-file pipeline to src/$PKG/defs/pipeline.py"
cat > "src/$PKG/defs/pipeline.py" <<'PYEOF'
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
    preds.to_csv("out/wine_predictions.csv", index=False)
    importance.to_csv("out/wine_importance.csv", index=False)
    context.log.info(f"wrote 2 CSVs; {len(preds)} predictions, {len(importance)} features")
    return preds, importance


# ── ASSET 3 (custom): cross-validation scores ─────────────────────────

@dg.asset(group_name="validation")
def wine_cv_scores(context: dg.AssetExecutionContext, wine_raw: pd.DataFrame) -> pd.DataFrame:
    scaled = _scale_features(wine_raw)
    df = _cross_validate(scaled)
    df.to_csv("out/wine_cv.csv", index=False)
    context.log.info(f"5-fold CV — mean test={df['test_score'].mean():.3f}")
    return df


# Merge the component's Definitions with the custom-code Definitions.
defs = dg.Definitions.merge(
    _ingest.build_defs(None),
    dg.Definitions(assets=[wine_model_outputs, wine_cv_scores]),
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
echo "See examples/wine_ml_pipeline_py_minimal.md for the full walkthrough."
