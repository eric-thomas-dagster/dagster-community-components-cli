#!/usr/bin/env bash
# JDE Orchestrator end-to-end demo — mock JDE AIS REST in Docker.
set -eo pipefail

PROJECT_DIR="${1:-jde-demo}"
JDE_PORT="${JDE_PORT:-9091}"
MOCK_IMAGE="${MOCK_IMAGE:-jde-orch-mock:demo}"
COMMIT_SHA="dbd28683"

if ! command -v docker >/dev/null 2>&1; then echo "✗ docker required"; exit 1; fi
if ! command -v uv >/dev/null 2>&1; then echo "✗ uv required"; exit 1; fi

MOCK_DIR="$PROJECT_ABS/jde-mock-src"
rm -rf "$MOCK_DIR" && mkdir -p "$MOCK_DIR"

cat > "$MOCK_DIR/mock_jde.py" <<'PYEOF'
"""Mock JDE Orchestrator REST API."""
import random
import time
from flask import Flask, jsonify, request

app = Flask(__name__)
JOB_COUNTER = {"n": 0}
JOBS = {}

_ORCHS = ["JDE_AR_Recon", "JDE_AP_Payment_Run", "JDE_Fetch_Open_APs", "JDE_LegacyReport_deprecated"]


@app.route("/jderest/v3/orchestrator", methods=["GET"])
def list_orchestrations():
    return jsonify({"orchestrations": [{"name": n} for n in _ORCHS]})


@app.route("/jderest/v3/orchestrator/<orch>", methods=["POST"])
def run_orch(orch):
    if orch not in _ORCHS:
        return jsonify({"error": "not found"}), 404

    if request.args.get("asynchronous", "").lower() == "true":
        JOB_COUNTER["n"] += 1
        jid = f"job-{JOB_COUNTER['n']:04d}"
        JOBS[jid] = {"orchestration": orch, "state": "RUNNING", "started_at": time.time()}
        return jsonify({"jobId": jid, "status": "SUBMITTED"})

    # Sync — return canned rows shape.
    return jsonify({
        "orchestration": orch,
        "status": "SUCCESS",
        "ServiceRequest1": {
            "RowSet": [
                {"invoice_id": f"INV-{i:04d}", "amount": round(random.uniform(100, 10000), 2),
                 "customer": f"Customer-{i % 5}"}
                for i in range(1, 11)
            ]
        },
    })


@app.route("/jderest/v3/orchestrator/status/<job_id>", methods=["GET"])
def job_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    # Move to SUCCESS after ~2s.
    if time.time() - job["started_at"] > 2 and job["state"] == "RUNNING":
        job["state"] = "SUCCESS"
    return jsonify({"jobId": job_id, "status": job["state"]})


@app.route("/jderest/v3/orchestrator/<orch>/history", methods=["GET"])
def history(orch):
    if orch not in _ORCHS:
        return jsonify({"error": "not found"}), 404
    # Return a single "last run" record with SUCCESS state.
    return jsonify({
        "history": [
            {"jobId": f"hist-{orch}-001", "status": "SUCCESS",
             "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        ]
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9091)
PYEOF

cat > "$MOCK_DIR/Dockerfile" <<'DOCKERFILEEOF'
FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir flask==3.0.0
COPY mock_jde.py .
EXPOSE 9091
CMD ["python", "mock_jde.py"]
DOCKERFILEEOF

echo ">>> Building mock JDE image ($MOCK_IMAGE)"
docker build -q -t "$MOCK_IMAGE" "$MOCK_DIR" >/dev/null

docker rm -f jde-orch-mock >/dev/null 2>&1 || true
echo ">>> Starting mock JDE on port $JDE_PORT"
docker run -d --name jde-orch-mock -p "$JDE_PORT:9091" "$MOCK_IMAGE" >/dev/null

for i in $(seq 1 30); do
  if curl -fsS -m 2 "http://localhost:$JDE_PORT/jderest/v3/orchestrator" -u demo:demo >/dev/null 2>&1; then
    echo "    ✓ Mock JDE ready after ${i}s"; break
  fi
  sleep 1
done

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
rm -rf "$PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"
PKG="$(ls src/ | head -1)"
uv add --dev -q dagster-dg-cli dagster-webserver

echo ">>> Installing jde components"
CLI="uvx --from dagster-community-components-cli dagster-component"
$CLI --refresh add jde_orchestrator_resource --auto-install
$CLI add jde_orchestration_trigger_job --auto-install
$CLI add jde_orchestration_status_sensor --auto-install
$CLI add jde_orchestration_output_ingestion --auto-install
$CLI add jde_orchestrator_workspace --auto-install

echo ">>> Wiring defs.yaml files"

cat > "src/$PKG/defs/jde_orchestrator_resource/defs.yaml" <<YAML
type: dagster_community_components.JDEOrchestratorResourceComponent
attributes:
  resource_key: jde_orchestrator_resource
  base_url_env_var: JDE_AIS_URL
  username_env_var: JDE_USER
  password_env_var: JDE_PASSWORD
  verify_ssl: false
YAML

cat > "src/$PKG/defs/jde_orchestration_trigger_job/defs.yaml" <<YAML
type: dagster_community_components.JDEOrchestrationTriggerJobComponent
attributes:
  job_name: run_ar_recon
  orchestration: JDE_AR_Recon
  inputs:
    AsOfDate: "2026-07-10"
    Currency: USD
  async_mode: true
  wait_for_completion: true
  poll_interval_seconds: 1
  timeout_seconds: 60
  resource_key: jde_orchestrator_resource
YAML

cat > "src/$PKG/defs/jde_orchestration_status_sensor/defs.yaml" <<YAML
type: dagster_community_components.JDEOrchestrationStatusSensorComponent
attributes:
  sensor_name: ar_recon_done
  orchestration: JDE_AR_Recon
  target_states: [SUCCESS]
  job_name: run_ar_recon
  resource_key: jde_orchestrator_resource
  minimum_interval_seconds: 30
  default_status: stopped
YAML

cat > "src/$PKG/defs/jde_orchestration_output_ingestion/defs.yaml" <<YAML
type: dagster_community_components.JDEOrchestrationOutputIngestionComponent
attributes:
  asset_key: open_ap_invoices
  orchestration: JDE_Fetch_Open_APs
  inputs:
    CompanyCode: "00001"
  output_field: "ServiceRequest1.RowSet"
  group_name: jde_finance
  resource_key: jde_orchestrator_resource
YAML

cat > "src/$PKG/defs/jde_orchestrator_workspace/defs.yaml" <<YAML
type: dagster_community_components.JDEOrchestratorWorkspaceComponent
attributes:
  base_url_env_var: JDE_AIS_URL
  username_env_var: JDE_USER
  password_env_var: JDE_PASSWORD
  verify_ssl: false
  orchestration_selector:
    by_pattern: ["JDE_*"]
    exclude_by_pattern: ["*_deprecated"]
  group_name: jde_workspace
  async_mode: true
  wait_for_completion: true
  poll_interval_seconds: 1
  timeout_seconds: 60
  defs_state:
    management_type: LOCAL_FILESYSTEM
    refresh_if_dev: true
YAML

export JDE_AIS_URL="http://localhost:$JDE_PORT"
export JDE_USER="demo"
export JDE_PASSWORD="demo"

cat > ".env" <<ENVEOF
JDE_AIS_URL=$JDE_AIS_URL
JDE_USER=$JDE_USER
JDE_PASSWORD=$JDE_PASSWORD
ENVEOF

DCC="dagster-community-components @ https://github.com/eric-thomas-dagster/dagster-component-templates/archive/$COMMIT_SHA.zip"

echo ">>> Validating defs (dg check)"
uv run --with "$DCC" dg check defs || { echo "    ✗ dg check failed"; exit 1; }

echo ">>> Triggering run_ar_recon (async, poll to SUCCESS)"
uv run --with "$DCC" dg launch --job run_ar_recon || { echo "    ✗ trigger failed"; exit 1; }

echo ">>> Materializing open_ap_invoices"
uv run --with "$DCC" dg launch --assets open_ap_invoices || { echo "    ✗ ingestion failed"; exit 1; }

echo ">>> Refreshing workspace state"
uv run --with "$DCC" dg utils refresh-defs-state || { echo "    ✗ refresh failed"; exit 1; }

echo ">>> Materializing a workspace-emitted asset"
uv run --with "$DCC" dg launch --assets 'jde/orchestration/JDE_AR_Recon' || { echo "    ✗ workspace asset failed"; exit 1; }

cat <<DONE

✓ JDE Orchestrator demo up.

Mock JDE AIS:      http://localhost:$JDE_PORT
Dagster project:   $(pwd)

Cleanup:
  docker rm -f jde-orch-mock
  docker rmi $MOCK_IMAGE
DONE
