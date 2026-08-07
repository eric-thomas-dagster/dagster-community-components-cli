#!/usr/bin/env bash
# Qlik Compose end-to-end demo — mock Compose REST in Docker.
set -eo pipefail

PROJECT_DIR="${1:-qlik-compose-demo}"
COMPOSE_PORT="${COMPOSE_PORT:-4443}"
MOCK_IMAGE="${MOCK_IMAGE:-qlik-compose-mock:demo}"
COMMIT_SHA="e195f1e1"

if ! command -v docker >/dev/null 2>&1; then echo "✗ docker required"; exit 1; fi
if ! command -v uv >/dev/null 2>&1; then echo "✗ uv required"; exit 1; fi

MOCK_DIR="$PROJECT_ABS/qlik-compose-mock-src"
rm -rf "$MOCK_DIR" && mkdir -p "$MOCK_DIR"

cat > "$MOCK_DIR/mock_compose.py" <<'PYEOF'
"""Mock Qlik Compose REST API."""
import random
import threading
import time
from flask import Flask, jsonify, request

app = Flask(__name__)
_LOCK = threading.Lock()

_CATALOG = {
    "FinanceDW": {
        "workflows": [
            {"name": "FullBuildAndPopulate", "state": "COMPLETED",
             "last_completion_time": "2026-07-10T02:00:00Z",
             "last_run_duration_seconds": 1420, "rows_loaded": 1_800_000, "error_count": 0},
            {"name": "IncrementalUpdate", "state": "COMPLETED",
             "last_completion_time": "2026-07-10T04:15:00Z",
             "last_run_duration_seconds": 320, "rows_loaded": 45_000, "error_count": 0},
            {"name": "LegacyReport_deprecated", "state": "STOPPED",
             "last_completion_time": "2026-06-01T00:00:00Z",
             "last_run_duration_seconds": 0, "rows_loaded": 0, "error_count": 0},
        ],
        "data_marts": [
            {"name": "MonthlyClose"}, {"name": "DailyForecast"},
        ],
    },
    "SalesDW": {
        "workflows": [
            {"name": "FullBuildAndPopulate", "state": "COMPLETED",
             "last_completion_time": "2026-07-10T02:30:00Z",
             "last_run_duration_seconds": 980, "rows_loaded": 2_400_000, "error_count": 0},
        ],
        "data_marts": [
            {"name": "PipelineHealth"}, {"name": "QuarterlyBookings"},
        ],
    },
}


@app.route("/qlikcompose/api/v1/login", methods=["POST"])
def login():
    body = request.get_json(silent=True) or {}
    if not body.get("username") or not body.get("password"):
        return jsonify({"error": "credentials required"}), 401
    return jsonify({"sessionId": "mock-session"})


@app.route("/qlikcompose/api/v1/projects", methods=["GET"])
def projects():
    with _LOCK:
        return jsonify({"projects": [{"name": p} for p in _CATALOG.keys()]})


@app.route("/qlikcompose/api/v1/projects/<project>/workflows", methods=["GET"])
def list_workflows(project):
    with _LOCK:
        if project not in _CATALOG:
            return jsonify({"error": "not found"}), 404
        return jsonify({"workflows": [{"name": w["name"]} for w in _CATALOG[project]["workflows"]]})


@app.route("/qlikcompose/api/v1/projects/<project>/data_marts", methods=["GET"])
def list_data_marts(project):
    with _LOCK:
        if project not in _CATALOG:
            return jsonify({"error": "not found"}), 404
        return jsonify({"data_marts": [{"name": m["name"]} for m in _CATALOG[project]["data_marts"]]})


@app.route("/qlikcompose/api/v1/projects/<project>/workflows/<workflow>", methods=["GET", "POST"])
def workflow_detail(project, workflow):
    with _LOCK:
        if project not in _CATALOG:
            return jsonify({"error": "not found"}), 404
        wf = next((w for w in _CATALOG[project]["workflows"] if w["name"] == workflow), None)
        if not wf:
            return jsonify({"error": "not found"}), 404

        if request.method == "POST":
            action = request.args.get("action", "").lower()
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if action == "run":
                wf["state"] = "RUNNING"
                wf["last_completion_time"] = now

                def _complete():
                    time.sleep(3)
                    with _LOCK:
                        wf["state"] = "COMPLETED"
                        wf["last_completion_time"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                        wf["last_run_duration_seconds"] = 3
                        wf["rows_loaded"] = random.randint(1000, 100000)

                threading.Thread(target=_complete, daemon=True).start()
            elif action == "stop":
                wf["state"] = "STOPPED"
                wf["last_completion_time"] = now
            return jsonify({"ok": True, "action": action})

        return jsonify({"workflow": wf})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=4443)
PYEOF

cat > "$MOCK_DIR/Dockerfile" <<'DOCKERFILEEOF'
FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir flask==3.0.0
COPY mock_compose.py .
EXPOSE 4443
CMD ["python", "mock_compose.py"]
DOCKERFILEEOF

echo ">>> Building mock Qlik Compose image ($MOCK_IMAGE)"
docker build -q -t "$MOCK_IMAGE" "$MOCK_DIR" >/dev/null

docker rm -f qlik-compose-mock >/dev/null 2>&1 || true
echo ">>> Starting mock Qlik Compose on port $COMPOSE_PORT"
docker run -d --name qlik-compose-mock -p "$COMPOSE_PORT:4443" "$MOCK_IMAGE" >/dev/null

for i in $(seq 1 30); do
  if curl -fsS -m 2 -X POST "http://localhost:$COMPOSE_PORT/qlikcompose/api/v1/login" \
      -H "Content-Type: application/json" -d '{"username":"demo","password":"demo"}' >/dev/null 2>&1; then
    echo "    ✓ Mock Compose ready after ${i}s"
    break
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

echo ">>> Installing qlik_compose components"
CLI="uvx --from dagster-community-components-cli dagster-component"
$CLI --refresh add qlik_compose_resource --auto-install
$CLI add qlik_compose_workflow_trigger_job --auto-install
$CLI add qlik_compose_workflow_status_sensor --auto-install
$CLI add qlik_compose_workflow_metrics_ingestion --auto-install
$CLI add qlik_compose_workspace --auto-install

echo ">>> Wiring defs.yaml files"

cat > "src/$PKG/defs/qlik_compose_resource/defs.yaml" <<YAML
type: dagster_community_components.QlikComposeResourceComponent
attributes:
  resource_key: qlik_compose_resource
  base_url_env_var: QLIK_COMPOSE_URL
  username_env_var: QLIK_COMPOSE_USER
  password_env_var: QLIK_COMPOSE_PASSWORD
  verify_ssl: false
YAML

cat > "src/$PKG/defs/qlik_compose_workflow_trigger_job/defs.yaml" <<YAML
type: dagster_community_components.QlikComposeWorkflowTriggerJobComponent
attributes:
  job_name: rebuild_finance_dw
  project: FinanceDW
  workflow: FullBuildAndPopulate
  action: run
  wait_for_completion: true
  poll_interval_seconds: 2
  timeout_seconds: 60
  resource_key: qlik_compose_resource
YAML

cat > "src/$PKG/defs/qlik_compose_workflow_status_sensor/defs.yaml" <<YAML
type: dagster_community_components.QlikComposeWorkflowStatusSensorComponent
attributes:
  sensor_name: finance_dw_done
  project: FinanceDW
  workflow: FullBuildAndPopulate
  target_states: [COMPLETED]
  job_name: rebuild_finance_dw
  resource_key: qlik_compose_resource
  minimum_interval_seconds: 30
  default_status: stopped
YAML

cat > "src/$PKG/defs/qlik_compose_workflow_metrics_ingestion/defs.yaml" <<YAML
type: dagster_community_components.QlikComposeWorkflowMetricsIngestionComponent
attributes:
  asset_key: qlik_compose_metrics
  projects: [FinanceDW, SalesDW]
  group_name: qlik_observability
  resource_key: qlik_compose_resource
YAML

cat > "src/$PKG/defs/qlik_compose_workspace/defs.yaml" <<YAML
type: dagster_community_components.QlikComposeWorkspaceComponent
attributes:
  base_url_env_var: QLIK_COMPOSE_URL
  username_env_var: QLIK_COMPOSE_USER
  password_env_var: QLIK_COMPOSE_PASSWORD
  verify_ssl: false
  projects: [FinanceDW, SalesDW]
  workflow_selector:
    by_pattern: ["FullBuild*", "Incremental*"]
  data_mart_selector:
    by_pattern: ["Monthly*", "Quarterly*"]
  group_name: qlik_compose_workspace
  wait_for_completion: true
  poll_interval_seconds: 2
  timeout_seconds: 60
  defs_state:
    management_type: LOCAL_FILESYSTEM
    refresh_if_dev: true
YAML

export QLIK_COMPOSE_URL="http://localhost:$COMPOSE_PORT"
export QLIK_COMPOSE_USER="demo"
export QLIK_COMPOSE_PASSWORD="demo"

cat > ".env" <<ENVEOF
QLIK_COMPOSE_URL=$QLIK_COMPOSE_URL
QLIK_COMPOSE_USER=$QLIK_COMPOSE_USER
QLIK_COMPOSE_PASSWORD=$QLIK_COMPOSE_PASSWORD
ENVEOF

DCC="dagster-community-components @ https://github.com/eric-thomas-dagster/dagster-component-templates/archive/$COMMIT_SHA.zip"

echo ">>> Validating defs (dg check)"
uv run --with "$DCC" dg check defs || { echo "    ✗ dg check failed"; exit 1; }

echo ">>> Triggering rebuild_finance_dw"
uv run --with "$DCC" dg launch --job rebuild_finance_dw || { echo "    ✗ trigger failed"; exit 1; }

echo ">>> Materializing qlik_compose_metrics"
uv run --with "$DCC" dg launch --assets qlik_compose_metrics || { echo "    ✗ metrics failed"; exit 1; }

echo ">>> Refreshing workspace state"
uv run --with "$DCC" dg utils refresh-defs-state || { echo "    ✗ refresh failed"; exit 1; }

echo ">>> Materializing a workspace-emitted workflow asset"
uv run --with "$DCC" dg launch --assets 'qlik_compose/FinanceDW/workflow/FullBuildAndPopulate' || { echo "    ✗ workspace asset failed"; exit 1; }

cat <<DONE

✓ Qlik Compose demo up.

Mock Compose server:  http://localhost:$COMPOSE_PORT
Dagster project:      $(pwd)

Cleanup:
  docker rm -f qlik-compose-mock
  docker rmi $MOCK_IMAGE
DONE
