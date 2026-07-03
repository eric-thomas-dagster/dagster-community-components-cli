#!/usr/bin/env bash
# setup_vercel_deployment_demo.sh
#
# Scaffolds a Dagster project that observes Vercel deployments via the
# real Vercel REST API using VercelDeploymentSensorComponent +
# ExternalVercelDeploymentAsset.
#
# What it demonstrates
#   • vercel_deployment_sensor — polls Vercel /v6/deployments, emits
#     AssetObservation on READY, fires downstream Dagster job.
#   • external_vercel_deployment — declares the deployment stream in
#     the Dagster catalog with clickable URLs.
#   • Live gRPC-style API auth: Bearer token via env var.
#
# Cost: $0 API-side (Vercel deployment reads are free).
#
# Requirements
#   • uv (https://docs.astral.sh/uv/)
#   • $VERCEL_API_TOKEN in your shell (create at
#     https://vercel.com/account/tokens with 'Read Access' scope)
#   • The env var $VERCEL_PROJECT_ID pointing at a project ID you own
#     (find on the Vercel dashboard → Project → Settings → General).
#
# Usage
#   export VERCEL_API_TOKEN=vck_...
#   export VERCEL_PROJECT_ID=prj_...
#   ./setup_vercel_deployment_demo.sh                          # → vercel_demo/
#   ./setup_vercel_deployment_demo.sh my_project               # custom name

set -eo pipefail

PROJECT_NAME="${1:-vercel_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_YELLOW='\033[0;33m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

[ -z "${VERCEL_API_TOKEN:-}" ] && fail "VERCEL_API_TOKEN not set. Get one at https://vercel.com/account/tokens"
[ -z "${VERCEL_PROJECT_ID:-}" ] && fail "VERCEL_PROJECT_ID not set. Find it at https://vercel.com/<team>/<project>/settings/general"
command -v uvx >/dev/null 2>&1 || fail "uvx not found. Install uv: https://docs.astral.sh/uv/"
[ -d "$PROJECT_DIR" ] && fail "Directory already exists: $PROJECT_DIR"

info "VERCEL_API_TOKEN: set (prefix ${VERCEL_API_TOKEN:0:8}…)"
info "VERCEL_PROJECT_ID: $VERCEL_PROJECT_ID"
info "Target project: $PROJECT_DIR"

info "Verifying Vercel API access…"
if ! curl -s -H "Authorization: Bearer $VERCEL_API_TOKEN" "https://api.vercel.com/v6/deployments?projectId=$VERCEL_PROJECT_ID&limit=1" | grep -q '"deployments"'; then
  fail "Vercel API check failed. Verify token has 'Read' access and project_id is correct."
fi
ok "Vercel API reachable"

info "Scaffolding Dagster project…"
uvx create-dagster project "$PROJECT_DIR" --uv-sync 2>&1 | tail -3 || fail "create-dagster failed"
cd "$PROJECT_DIR"

info "Installing deps (dagster-community-components + requests)…"
if [ -n "${DCC_SRC:-}" ] && [ -d "$DCC_SRC" ]; then
  info "Using local DCC source: $DCC_SRC"
  uv add --quiet "dagster-community-components @ ${DCC_SRC}" 'requests>=2.28' || fail "uv add failed"
else
  uv add --quiet 'dagster-community-components>=0.10.0' 'requests>=2.28' || fail "uv add failed"
fi
ok "Dependencies installed"

# ── defs.yaml files ─────────────────────────────────────────────────────────
mkdir -p "src/${PROJECT_NAME}/defs/external" "src/${PROJECT_NAME}/defs/sensor"

cat > "src/${PROJECT_NAME}/defs/external/defs.yaml" <<YAML
type: dagster_community_components.ExternalVercelDeploymentAsset
attributes:
  asset_key: vercel/site/production
  project_id: ${VERCEL_PROJECT_ID}
  target: production
  group_name: vercel
  description: "Vercel production deployments (observed via vercel_deployment_sensor)"
YAML

cat > "src/${PROJECT_NAME}/defs/sensor/defs.yaml" <<YAML
type: dagster_community_components.VercelDeploymentSensorComponent
attributes:
  sensor_name: vercel_prod_ready
  project_id: ${VERCEL_PROJECT_ID}
  target: production
  api_token_env_var: VERCEL_API_TOKEN
  job_name: __ASSET_JOB
  asset_key: vercel/site/production
  asset_event_type: observation
  minimum_interval_seconds: 60
YAML
ok "Wrote defs.yaml (external asset + sensor)"

info "Running the sensor once against the real Vercel API…"
# The external asset is declare-only (AssetSpec) — Dagster can't 'materialize'
# it via CLI. Instead we invoke the sensor programmatically to confirm the
# component works end-to-end against your real Vercel account.
cat > /tmp/_vercel_sensor_tick.py <<'PYTICK'
import sys, warnings
warnings.filterwarnings("ignore", message=".*shadows an attribute.*")
sys.path.insert(0, "src")
from dagster_community_components import VercelDeploymentSensorComponent
from dagster._core.definitions.sensor_definition import SensorEvaluationContext
from dagster._core.instance import DagsterInstance
import os

comp = VercelDeploymentSensorComponent(
    sensor_name="vercel_prod_ready",
    project_id=os.environ["VERCEL_PROJECT_ID"],
    target="production",
    api_token_env_var="VERCEL_API_TOKEN",
    job_name="dummy",
    asset_key="vercel/site/production",
    asset_event_type="observation",
)
defs = comp.build_defs(None)
sd = defs.sensors[0]
ctx = SensorEvaluationContext(
    instance_ref=None, last_tick_completion_time=None, last_run_key=None,
    cursor=None, repository_name=None,
    instance=DagsterInstance.ephemeral(), sensor_name=sd.name,
)
result = sd(ctx)
print("--- Sensor tick result ---")
print("skip_reason:", getattr(result, "skip_reason", None))
print("run_requests:", len(getattr(result, "run_requests", []) or []))
for ae in (getattr(result, "asset_events", None) or []):
    print("asset_event:", ae.asset_key, "→", ae.description)
    for k, v in (ae.metadata or {}).items():
        print(f"  {k}: {str(v)[:120]}")
PYTICK
uv run python /tmp/_vercel_sensor_tick.py 2>&1 | tail -20 || fail "sensor tick failed"
rm -f /tmp/_vercel_sensor_tick.py
ok "Sensor observed real Vercel deployment"

echo
ok "Demo complete."
echo
cat <<EOF
Next steps:
  cd $PROJECT_NAME
  uv run dg dev                # open the Dagster UI

In the UI:
  • Browse to the 'vercel' asset group — you'll see vercel/site/production
    with clickable dashboard + production URLs in its metadata.
  • Find the 'vercel_prod_ready' sensor — click 'Tick now' to run one
    poll against the real Vercel API. It'll emit an AssetObservation
    against vercel/site/production with the latest READY deployment's
    uid, commit sha, branch, and message.
  • Set the sensor to 'Running' for continuous polling (60s cadence
    is safe for Vercel's rate limits).

Chain to downstream Dagster assets:
  Any asset with 'deps: ["vercel/site/production"]' will show a lineage
  edge from Vercel; use it to gate post-deploy smoke tests, cache
  warmers, etc. on production deployments being live.
EOF
