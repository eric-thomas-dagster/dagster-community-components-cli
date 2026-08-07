#!/usr/bin/env bash
# Wine ML Pipeline demo — one MLPipelineComponent.
#
# Same 6-step logic as the other wine variants, collapsed into ONE YAML
# file using MLPipelineComponent (sibling of polars_pipeline,
# warehouse_pipeline, pyspark_pipeline, snowpark_pipeline).
#
# 3 first-class asset outputs (wine_ml_preds, wine_ml_imp, wine_ml_cv)
# emitted from one component. Three CSVs written as side effects.
#
# See wine_ml_pipeline_component.md for the full walkthrough +
# comparison against the other wine variants.

set -euo pipefail

PROJECT_DIR="${1:-wine-ml-pipeline-component-demo}"

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

echo ">>> Writing single defs.yaml — the whole ML pipeline"
mkdir -p "src/$PKG/defs/wine_ml"
cat > "src/$PKG/defs/wine_ml/defs.yaml" <<'YAMLEOF'
type: dagster_community_components.MLPipelineComponent
attributes:
  asset_name_prefix: wine_ml
  group_name: ml

  source:
    kind: url
    url: "https://archive.ics.uci.edu/ml/machine-learning-databases/wine-quality/winequality-red.csv"
    delimiter: ";"

  target_column: quality
  feature_columns:
    - "fixed acidity"
    - "volatile acidity"
    - "citric acid"
    - "residual sugar"
    - "chlorides"
    - "free sulfur dioxide"
    - "total sulfur dioxide"
    - "density"
    - "pH"
    - "sulphates"
    - "alcohol"

  steps:
    - {id: scaled,  op: scale, method: standard}
    - {id: split,   op: split, test_size: 0.2, stratify_column: quality, random_state: 42}
    - {id: trained, op: train, model_type: decision_tree, task_type: classification,
                    params: {max_depth: 6, random_state: 42}}
    - {id: preds,   op: predict, model: trained, input: scaled}
    - {id: imp,     op: importance, model: trained}
    - {id: cv,      op: cross_validate, source: scaled,
                    model_type: decision_tree, task_type: classification,
                    params: {max_depth: 6, random_state: 42}, cv: 5}

  outputs:
    assets: [preds, imp, cv]
    csv_sinks:
      - {from: preds, path: out/wine_predictions.csv}
      - {from: imp,   path: out/wine_importance.csv}
      - {from: cv,    path: out/wine_cv.csv}
YAMLEOF

echo ""
echo ">>> Setup complete."
echo ""
echo "Materialize (open UI):"
echo "    cd $PROJECT_DIR && uv run dg dev"
echo "        → http://localhost:3000 — 3 asset nodes (wine_ml_preds, _imp, _cv)"
echo ""
echo "Or headless:"
echo "    cd $PROJECT_DIR && uv run dg launch --assets '*'"
echo "    ls -la $PROJECT_ABS/out/wine_*.csv"
echo ""
echo "See examples/wine_ml_pipeline_component.md for the full walkthrough."
