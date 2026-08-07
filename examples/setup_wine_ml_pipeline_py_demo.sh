#!/usr/bin/env bash
# Wine ML Pipeline demo — single-file Python variant.
#
# Same pipeline as setup_wine_ml_pipeline_demo.sh but wired via one Python
# file instead of nine defs.yaml files. Useful for teams migrating from
# single-script frameworks (Prefect, Airflow decorators) who prefer
# everything visible in one file.
#
# Pipeline (9 component instances, all in src/<pkg>/defs/pipeline.py):
#   file_ingestion → feature_scaler ─┬─→ create_samples → decision_tree ─┬─→ CSV (predictions)
#                                    │                                   └─→ CSV (feature_importance)
#                                    └─→ cross_validation ─────────────────→ CSV (cv_scores)
#
# See wine_ml_pipeline_py.md for the full walkthrough.

set -euo pipefail

PROJECT_DIR="${1:-wine-ml-pipeline-py-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"
mkdir -p out
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas scikit-learn requests dagster-community-components
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

echo ">>> Writing single-file pipeline to src/$PKG/defs/pipeline.py"
cat > "src/$PKG/defs/pipeline.py" <<'PYEOF'
"""Wine ML pipeline — pure Python variant.

Uses the same six community components as the sibling YAML walkthrough
(wine_ml_pipeline) but wires them via direct class instantiation instead
of per-component defs.yaml files.
"""
from dagster import Definitions
from dagster_community_components import (
    FileIngestionComponent,
    FeatureScalerComponent,
    CreateSamplesComponent,
    DecisionTreeModelComponent,
    CrossValidationComponent,
    DataframeToCsvComponent,
)

# 11 chemistry features. Shared by scaler, model, and CV configs.
FEATURES = [
    "fixed acidity", "volatile acidity", "citric acid", "residual sugar",
    "chlorides", "free sulfur dioxide", "total sulfur dioxide", "density",
    "pH", "sulphates", "alcohol",
]

components = [
    # 1. Ingest — pull UCI red wine quality dataset from a public URL.
    FileIngestionComponent(
        asset_name="wine_raw",
        file_path="https://archive.ics.uci.edu/ml/machine-learning-databases/wine-quality/winequality-red.csv",
        description="UCI red wine quality dataset (1599 rows, 11 chemistry features).",
        delimiter=";",
        group_name="ingest",
    ),

    # 2. Standardize chemistry features → zero mean, unit variance.
    FeatureScalerComponent(
        asset_name="wine_scaled",
        upstream_asset_key="wine_raw",
        strategy="standard",
        columns=FEATURES,
        group_name="transform",
    ),

    # 3. 80/20 train/test split, stratified on quality.
    CreateSamplesComponent(
        asset_name="wine_split",
        upstream_asset_key="wine_scaled",
        test_size=0.2,
        random_state=42,
        stratify_column="quality",
        output_split_column="split",
        group_name="transform",
    ),

    # 4. Model — predictions branch.
    DecisionTreeModelComponent(
        asset_name="wine_predictions",
        upstream_asset_key="wine_split",
        target_column="quality",
        feature_columns=FEATURES,
        task_type="classification",
        max_depth=6,
        random_state=42,
        output_mode="predictions",
        group_name="model",
    ),

    # 5. Model — feature-importance branch (same fit, different output).
    DecisionTreeModelComponent(
        asset_name="wine_feature_importance",
        upstream_asset_key="wine_split",
        target_column="quality",
        feature_columns=FEATURES,
        task_type="classification",
        max_depth=6,
        random_state=42,
        output_mode="feature_importance",
        group_name="model",
    ),

    # 6. Cross-validation on the pre-split, scaled data (independent branch).
    CrossValidationComponent(
        asset_name="wine_cv_scores",
        upstream_asset_key="wine_scaled",
        target_column="quality",
        feature_columns=FEATURES,
        model_type="decision_tree",
        task_type="classification",
        cv_folds=5,
        random_state=42,
        group_name="validation",
    ),

    # 7-9. Three CSV sinks — same class, three instances, different upstream + path.
    DataframeToCsvComponent(
        asset_name="predictions_report",
        upstream_asset_key="wine_predictions",
        file_path="out/wine_predictions.csv",
        group_name="sink",
    ),
    DataframeToCsvComponent(
        asset_name="importance_report",
        upstream_asset_key="wine_feature_importance",
        file_path="out/wine_importance.csv",
        group_name="sink",
    ),
    DataframeToCsvComponent(
        asset_name="cv_report",
        upstream_asset_key="wine_cv_scores",
        file_path="out/wine_cv.csv",
        group_name="sink",
    ),
]

# Merge each component's Definitions into one top-level Definitions.
defs = Definitions.merge(*[c.build_defs(None) for c in components])
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
echo "See examples/wine_ml_pipeline_py.md for the full walkthrough."
