#!/usr/bin/env bash
# setup_mlflow_pipeline_demo.sh
#
# End-to-end MLflow pipeline demo — showcases all 7 community MLflow components
# working together in one Dagster project, backed by a local file-store MLflow
# tracking backend (no server, no docker).
#
# What it demonstrates
#   • mlflow_resource            — shared MLflow connection
#   • mlflow_workspace           — auto-enumerate registered models as assets
#   • mlflow_experiment_sensor   — react to new experiment runs
#   • mlflow_model_sensor        — react to new Model Registry promotions
#   • mlflow_model_version_check — gate on Production model existing
#   • mlflow_model_promotion     — auditable Staging → Production transition
#   • mlflow_model_inference     — score data with the promoted model
#
# The whole thing lives inside the project dir (mlflow_store/, mlflow.db) so
# it's self-contained and Windows-safe (no /tmp paths).
#
# Cost: $0. No credentials.
#
# Requirements
#   • uv (https://docs.astral.sh/uv/)
#
# Usage
#   ./setup_mlflow_pipeline_demo.sh                     # → mlflow_demo/
#   ./setup_mlflow_pipeline_demo.sh my_pipeline         # custom name

set -eo pipefail

PROJECT_NAME="${1:-mlflow_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

command -v uvx >/dev/null 2>&1 || fail "uvx not found. Install uv: https://docs.astral.sh/uv/"
[ -d "$PROJECT_DIR" ] && fail "Directory already exists: $PROJECT_DIR"

info "Target project: $PROJECT_DIR"

# ── Scaffold Dagster project ────────────────────────────────────────────────
info "Scaffolding Dagster project…"
uvx create-dagster project "$PROJECT_DIR" --uv-sync 2>&1 | tail -3 || fail "create-dagster failed"
cd "$PROJECT_DIR"

info "Installing deps (mlflow + scikit-learn + pandas)…"
if [ -n "${DCC_SRC:-}" ] && [ -d "$DCC_SRC" ]; then
  info "Using local DCC source: $DCC_SRC"
  uv add --quiet \
    "dagster-community-components @ ${DCC_SRC}" \
    "mlflow>=2.0.0" \
    "scikit-learn>=1.3.0" \
    "pandas>=1.5.0" \
    || fail "uv add failed"
else
  uv add --quiet \
    "dagster-community-components @ git+https://github.com/eric-thomas-dagster/dagster-component-templates.git" \
    "mlflow>=2.0.0" \
    "scikit-learn>=1.3.0" \
    "pandas>=1.5.0" \
    || fail "uv add failed"
fi
ok "Dependencies installed"

# ── Local MLflow tracking store (sqlite backend, self-contained) ───────────
# MLflow 3.x deprecates the file backend; sqlite works fully and lives in the
# project dir (Windows-safe, no /tmp paths).
mkdir -p mlflow_artifacts
export MLFLOW_TRACKING_URI="sqlite:///${PROJECT_DIR}/mlflow.db"
export MLFLOW_ARTIFACT_URI="file://${PROJECT_DIR}/mlflow_artifacts"
info "MLflow tracking URI: $MLFLOW_TRACKING_URI"

# ── Seed MLflow with a trained model registered in Staging ─────────────────
info "Training a churn model + registering it in MLflow Staging…"
uv run python <<PY
import os
import mlflow
import mlflow.sklearn
import pandas as pd
from pathlib import Path
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split

# ── Tracking store ─────────────────────────────────────────────────────
tracking_uri = os.environ["MLFLOW_TRACKING_URI"]
mlflow.set_tracking_uri(tracking_uri)
mlflow.set_experiment("churn_prediction")

# ── Synthetic churn training data ──────────────────────────────────────
import numpy as np
rng = np.random.default_rng(42)
n = 500
df = pd.DataFrame({
    "tenure_months": rng.integers(1, 60, size=n),
    "monthly_charges": rng.uniform(20, 120, size=n),
    "total_charges": rng.uniform(50, 5000, size=n),
    "support_tickets": rng.integers(0, 8, size=n),
})
# churn ~= high monthly + low tenure + tickets
df["churned"] = (
    (df["monthly_charges"] > 80).astype(int)
    + (df["tenure_months"] < 12).astype(int)
    + (df["support_tickets"] > 3).astype(int)
    >= 2
).astype(int)

X = df.drop(columns=["churned"])
y = df["churned"]
X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.3, random_state=42)

# ── Log a training run ─────────────────────────────────────────────────
with mlflow.start_run() as run:
    model = LogisticRegression(max_iter=1000, random_state=42)
    model.fit(X_train, y_train)
    train_score = model.score(X_train, y_train)
    test_score = model.score(X_test, y_test)

    mlflow.log_param("model_type", "LogisticRegression")
    mlflow.log_param("max_iter", 1000)
    mlflow.log_metric("train_accuracy", train_score)
    mlflow.log_metric("test_accuracy", test_score)
    mlflow.log_metric("training_rows", len(X_train))

    # Log + register — puts v1 in Staging
    mlflow.sklearn.log_model(
        model,
        artifact_path="model",
        registered_model_name="churn_model",
    )

    run_id = run.info.run_id
    print(f"  MLflow run: {run_id}")
    print(f"  Train accuracy: {train_score:.3f}")
    print(f"  Test accuracy:  {test_score:.3f}")

# ── Move v1 to Staging (starts in "None") ─────────────────────────────
client = mlflow.tracking.MlflowClient(tracking_uri=tracking_uri)
versions = client.get_latest_versions("churn_model")
if versions:
    latest = max(versions, key=lambda v: int(v.version))
    if latest.current_stage != "Staging":
        client.transition_model_version_stage(
            name="churn_model",
            version=latest.version,
            stage="Staging",
        )
        print(f"  Registered: churn_model v{latest.version} → Staging")
    else:
        print(f"  Registered: churn_model v{latest.version} (already Staging)")

# ── Also seed a small features CSV for the inference asset ────────────
features_path = Path("customer_features.csv")
features = pd.DataFrame({
    "customer_id": [f"C{i:03d}" for i in range(1, 21)],
    "tenure_months": rng.integers(1, 60, size=20),
    "monthly_charges": rng.uniform(20, 120, size=20),
    "total_charges": rng.uniform(50, 5000, size=20),
    "support_tickets": rng.integers(0, 8, size=20),
})
features.to_csv(features_path, index=False)
print(f"  Wrote {features_path} ({len(features)} rows)")
PY
[ $? -eq 0 ] || fail "MLflow training script failed"
ok "MLflow seeded with churn_model v1 (Staging)"

# ── Wire up the 6 defs.yaml files (using all 4 new + 2 existing components) ─
PKG="${PROJECT_NAME}"
mkdir -p "src/${PKG}/defs"/{mlflow_workspace,version_check,promotion,inference,inference_upstream}

# 1. Workspace — enumerates registered models as read-only assets
cat > "src/${PKG}/defs/mlflow_workspace/defs.yaml" <<YAML
type: dagster_community_components.MLflowWorkspaceComponent
attributes:
  tracking_uri_env_var: MLFLOW_TRACKING_URI
  model_selector:
    by_pattern: ["*"]
  experiment_selector:
    by_pattern: ["*"]
  defs_state:
    management_type: LOCAL_FILESYSTEM
    refresh_if_dev: true
YAML

# 2. Upstream: the customer_features CSV as an asset (source for inference)
cat > "src/${PKG}/defs/inference_upstream/defs.yaml" <<YAML
type: dagster_community_components.DataframeFromCsvComponent
attributes:
  asset_name: customer_features
  file_path: "{{ project_root }}/customer_features.csv"
YAML

# 3. Promotion asset: Staging → Production
cat > "src/${PKG}/defs/promotion/defs.yaml" <<YAML
type: dagster_community_components.MLflowModelPromotionComponent
attributes:
  asset_name: promote_churn_to_production
  tracking_uri_env_var: MLFLOW_TRACKING_URI
  model_name: churn_model
  source_stage: Staging
  target_stage: Production
  archive_existing_target: true
  group_name: mlflow_cd
YAML

# 4. Inference asset: uses the Production model to score customer_features
cat > "src/${PKG}/defs/inference/defs.yaml" <<YAML
type: dagster_community_components.MLflowModelInferenceComponent
attributes:
  asset_name: daily_churn_scores
  upstream_asset_key: customer_features
  tracking_uri_env_var: MLFLOW_TRACKING_URI
  model_name: churn_model
  model_stage: Production
  output_column: churn_score
  keep_input_columns: true
  id_columns: [customer_id]
  group_name: ml_scoring
YAML

# 5. Asset check: gate on Production model existing (blocks inference if missing)
cat > "src/${PKG}/defs/version_check/defs.yaml" <<YAML
type: dagster_community_components.MLflowModelVersionCheckComponent
attributes:
  asset_key: daily_churn_scores
  check_name: churn_production_model_exists
  tracking_uri_env_var: MLFLOW_TRACKING_URI
  model_name: churn_model
  required_stage: Production
  severity: ERROR
YAML

ok "Wrote 5 defs.yaml files (workspace + upstream + promotion + inference + check)"

# ── Validate ────────────────────────────────────────────────────────────────
info "Running dg check defs…"
export MLFLOW_TRACKING_URI  # must be visible to dg
uv run dg check defs 2>&1 | tail -8 || fail "dg check defs failed"
ok "Definitions validated"


# ── Final message ───────────────────────────────────────────────────────────
cat <<EOF

$(ok "Project scaffolded at: $PROJECT_DIR")

  MLflow tracking URI: $MLFLOW_TRACKING_URI

Next steps — run everything:

  cd $PROJECT_NAME
  export MLFLOW_TRACKING_URI="$MLFLOW_TRACKING_URI"
  uv run dg dev

In the UI (http://localhost:3000):

  1. Assets tab shows:
       • customer_features        (features CSV)
       • daily_churn_scores       (waits for Production model)
       • promote_churn_to_production   (Staging → Production action)
       • plus workspace-discovered assets for the churn_model registry entry
  2. Materialize promote_churn_to_production → moves v1 to Production
  3. Materialize daily_churn_scores → loads the Production model + scores
  4. Watch the churn_production_model_exists check pass on the score asset

Or run headless end-to-end:

  cd $PROJECT_NAME
  export MLFLOW_TRACKING_URI="$MLFLOW_TRACKING_URI"
  uv run dg launch --assets 'promote_churn_to_production,daily_churn_scores'

  # inspect the scored output
  uv run python -c "
import pickle, pandas as pd
from pathlib import Path
# Dagster's default IO manager pickles outputs; you'll see rows w/ churn_score
"

EOF
