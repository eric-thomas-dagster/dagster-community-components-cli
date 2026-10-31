#!/usr/bin/env bash
# setup_mlops_drift_guardrail_demo.sh
#
# A model-serving pipeline shouldn't just score data -- it should
# continuously verify it's running the RIGHT model AND watch for drift in
# its own outputs. This demo chains MLflowModelInferenceComponent (load a
# registered model + score a batch) with MLflowModelVersionCheckComponent
# (asset check: is the expected model stage actually deployed?) and
# AnomalyDetectionComponent (z-score outlier detection on the model's own
# output column).
#
#   customer_metrics → churn_scores → churn_score_anomalies
#       (synth)      (MLflow scoring)    (anomaly detection)
#                          |
#                   churn_model_prod_exists (asset check)
#
# Real local MLflow registry (sqlite:///mlflow.db) -- no cloud account, no
# MLflow server to run. A tiny real sklearn LogisticRegression is trained
# and registered BEFORE the Dagster project touches it, at the Production
# stage.
#
# REAL `dg` BUG FOUND: a purely-numeric string value like
# `model_version: "1"` gets silently re-coerced to an int by dg's YAML
# loader before it reaches the component's Pydantic model -- confirmed not
# fixable from the YAML side (plain quotes AND an explicit `!!str` tag both
# failed identically with the same pydantic_core ValidationError). This
# looks like the same class of bug the agent-memory demo's `formula` step
# hit (a quoted string literal getting mangled somewhere in dg's
# YAML-to-Pydantic resolution pipeline), manifesting differently here as a
# wrong TYPE rather than stripped quotes. Workaround used below:
# `model_stage: Production` + an explicit stage transition instead of a
# bare `model_version` pin -- "Production" isn't numeric-looking, so dg's
# loader leaves it alone. MLflow's model-registry "stages" are deprecated
# since 2.9 in favor of aliases but still fully functional (one
# FutureWarning, no error) as of mlflow 3.17.
#
# What it demonstrates
#   • synthetic_data_generator — per-customer churn metrics (orders, revenue, lifetime)
#   • mlflow_model_inference — loads the Production-staged model, scores the batch
#   • mlflow_model_version_check — asset check: Production model actually exists
#   • anomaly_detection — z-score outliers in the model's own churn_score output
#
# Cost: $0 — everything runs against a local sqlite-backed MLflow registry.
#
# Requirements
#   • uv (https://docs.astral.sh/uv/)
#
# Usage
#   ./setup_mlops_drift_guardrail_demo.sh                  # → mlops_drift_guardrail_demo/
#   ./setup_mlops_drift_guardrail_demo.sh my_project        # custom name

set -eo pipefail

PROJECT_NAME="${1:-mlops_drift_guardrail_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

command -v uvx >/dev/null 2>&1 || fail "uvx not found."
[ -d "$PROJECT_DIR" ] && fail "Directory exists: $PROJECT_DIR"

info "Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

info "Installing deps (pandas, numpy, mlflow, scikit-learn)…"
uv add -q pandas numpy mlflow scikit-learn
uv add --dev -q dagster-dg-cli dagster-webserver

info "Installing components (--refresh on first call busts the registry-manifest cache)"
uvx --from dagster-community-components-cli dagster-component --refresh add synthetic_data_generator --auto-install
uvx --from dagster-community-components-cli dagster-component add mlflow_model_inference --auto-install
uvx --from dagster-community-components-cli dagster-component add mlflow_model_version_check --auto-install
uvx --from dagster-community-components-cli dagster-component add anomaly_detection --auto-install

info "Training + registering a real local MLflow model (sqlite:///mlflow.db)"
cat > train_and_register_model.py <<'PYEOF'
"""Pre-seeds a real, local MLflow model registry (sqlite backend -- no cloud
account, no server to run) before Dagster assets try to load from it."""
import mlflow
import mlflow.sklearn
from sklearn.linear_model import LogisticRegression
import pandas as pd
import numpy as np

mlflow.set_tracking_uri("sqlite:///mlflow.db")

rng = np.random.default_rng(42)
n = 200
X = pd.DataFrame({
    "total_orders": rng.integers(1, 40, n),
    "total_revenue": rng.uniform(20, 6000, n),
    "lifetime_days": rng.integers(60, 800, n),
})
y = ((X["total_orders"] < 8) & (X["total_revenue"] < 800)).astype(int)

model = LogisticRegression().fit(X, y)

with mlflow.start_run():
    mlflow.sklearn.log_model(model, "model", registered_model_name="churn_model")

client = mlflow.tracking.MlflowClient()
client.transition_model_version_stage("churn_model", "1", "Production")
print("Registered churn_model version 1 and transitioned to Production")
PYEOF
uv run python3 train_and_register_model.py

info "Writing defs.yaml (synth → score [MLflow] → version check + anomaly detection)"

cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: customer_metrics
  schema_type: customer_churn_metrics
  row_count: 50
  random_state: 7
  group_name: source
EOF

cat > "src/$PKG/defs/mlflow_model_inference/defs.yaml" <<EOF
type: $PKG.components.mlflow_model_inference.component.MLflowModelInferenceComponent
attributes:
  asset_name: churn_scores
  upstream_asset_key: customer_metrics
  tracking_uri_env_var: MLFLOW_TRACKING_URI
  model_name: churn_model
  model_stage: Production
  feature_columns: [total_orders, total_revenue, lifetime_days]
  output_column: churn_score
  group_name: ml_scoring
EOF

cat > "src/$PKG/defs/mlflow_model_version_check/defs.yaml" <<EOF
type: $PKG.components.mlflow_model_version_check.component.MLflowModelVersionCheckComponent
attributes:
  asset_key: churn_scores
  check_name: churn_model_prod_exists
  tracking_uri_env_var: MLFLOW_TRACKING_URI
  model_name: churn_model
  required_stage: Production
  severity: ERROR
EOF

cat > "src/$PKG/defs/anomaly_detection/defs.yaml" <<EOF
type: $PKG.components.anomaly_detection.component.AnomalyDetectionComponent
attributes:
  asset_name: churn_score_anomalies
  upstream_asset_key: churn_scores
  detection_method: z_score
  metric_column: churn_score
  threshold: 2.0
  group_name: monitoring
EOF

ok "Wrote 4 defs.yaml"

export MLFLOW_TRACKING_URI="sqlite:///mlflow.db"

info "Verifying the project loads"
uv run dg check defs 2>&1 | tail -5

info "Materializing the full chain"
uv run dg launch --assets '*' 2>&1 | tail -20

echo
ok "Demo complete."
echo
cat <<EOF
The pipeline just ran:
  1. Generated 50 synthetic customer churn metrics (orders, revenue, lifetime)
  2. Loaded the Production-staged churn_model from the local MLflow registry
     and scored all 50 customers (churn_scores)
  3. Checked that a Production version of churn_model actually exists
     (churn_model_prod_exists asset check) -- this is what would block a
     downstream job if someone forgot to promote a model
  4. Ran z-score anomaly detection on the model's own churn_score output
     (churn_score_anomalies) -- flags customers the model scored unusually
     confidently, a building block for real drift monitoring

Inspect the results:
  cd $PROJECT_NAME
  MLFLOW_TRACKING_URI=sqlite:///mlflow.db uv run dg dev
    → click churn_scores → see the asset check passed, scores attached
    → click churn_score_anomalies → see which customers were flagged

Point at a real MLflow server:
  Set MLFLOW_TRACKING_URI to a real tracking server URI instead of the local
  sqlite file, and skip train_and_register_model.py -- use whatever model
  your team already has registered.

Production variants:
  • Swap anomaly_detection's detection_method/threshold to tune sensitivity
  • Add max_age_days to mlflow_model_version_check to also gate on model
    freshness (fail if the Production model hasn't been updated recently)
  • Partition customer_metrics/churn_scores daily to track drift over time
    instead of a single snapshot
EOF
