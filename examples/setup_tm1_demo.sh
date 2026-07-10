#!/usr/bin/env bash
# TM1 (IBM Planning Analytics) end-to-end demo — mock TM1 REST server in Docker.
#
# WHAT THIS DEMONSTRATES
#   The five TM1 components:
#     1. tm1_resource                — shared REST auth
#     2. tm1_process_trigger_job     — execute a TI process
#     3. tm1_process_status_sensor   — event-drive on process status
#     4. tm1_cube_data_ingestion     — cube slice via MDX → DataFrame
#     5. tm1_workspace               — auto-emit one asset per Cube/Process/Chore
#
# COST: $0 — mock in Docker. Pulls python:3.11-slim (~130 MB) on first run.

set -eo pipefail

PROJECT_DIR="${1:-tm1-demo}"
TM1_PORT="${TM1_PORT:-5495}"
MOCK_IMAGE="${MOCK_IMAGE:-tm1-mock:demo}"
COMMIT_SHA="a3c57901"

# --- 0. Tool check -------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then echo "✗ docker required"; exit 1; fi
if ! command -v uv >/dev/null 2>&1; then echo "✗ uv required"; exit 1; fi

# --- 1. Build the mock TM1 -----------------------------------------------
MOCK_DIR="/tmp/tm1-mock-src"
rm -rf "$MOCK_DIR" && mkdir -p "$MOCK_DIR"

cat > "$MOCK_DIR/mock_tm1.py" <<'PYEOF'
"""Mock TM1 REST API for the tm1 demo."""
import random
import threading
import time

from flask import Flask, jsonify, request

app = Flask(__name__)
_LOCK = threading.Lock()

_CATALOG = {
    "cubes": ["Sales", "Finance", "HeadCount", "TestCube"],
    "processes": [
        {"name": "LoadActualsFromGL",
         "status": "CompletedSuccessfully",
         "execution_id": "exec-001"},
        {"name": "RefreshHeadCountCube",
         "status": "CompletedSuccessfully",
         "execution_id": "exec-002"},
        {"name": "LegacyReport_deprecated",
         "status": "CompletedSuccessfully",
         "execution_id": "exec-003"},
    ],
    "chores": [
        {"name": "DailyRebuild", "status": "CompletedSuccessfully"},
        {"name": "HourlyRefresh", "status": "CompletedSuccessfully"},
    ],
}


@app.route("/api/v1/Configuration", methods=["GET"])
def config():
    return jsonify({"ServerName": "MockTM1", "ProductVersion": "12.4.0"})


@app.route("/api/v1/Cubes", methods=["GET"])
def cubes():
    with _LOCK:
        return jsonify({"value": [{"Name": c} for c in _CATALOG["cubes"]]})


@app.route("/api/v1/Processes", methods=["GET"])
def processes():
    with _LOCK:
        return jsonify({"value": [{"Name": p["name"]} for p in _CATALOG["processes"]]})


@app.route("/api/v1/Chores", methods=["GET"])
def chores():
    with _LOCK:
        return jsonify({"value": [{"Name": c["name"]} for c in _CATALOG["chores"]]})


@app.route("/api/v1/Processes('<name>')/tm1.ExecuteProcess", methods=["POST"])
@app.route("/api/v1/Processes(<path:name_quoted>)/tm1.ExecuteProcess", methods=["POST"])
def execute_process(name=None, name_quoted=None):
    proc_name = name or (name_quoted.strip("'") if name_quoted else "")
    return jsonify({
        "ProcessExecuteStatusCode": "CompletedSuccessfully",
        "ProcessName": proc_name,
        "ExecutedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })


@app.route("/api/v1/Chores('<name>')/tm1.Execute", methods=["POST"])
@app.route("/api/v1/Chores(<path:name_quoted>)/tm1.Execute", methods=["POST"])
def execute_chore(name=None, name_quoted=None):
    chore_name = name or (name_quoted.strip("'") if name_quoted else "")
    return jsonify({
        "Status": "CompletedSuccessfully",
        "ChoreName": chore_name,
    })


@app.route("/api/v1/Processes(<path:name_quoted>)", methods=["GET"])
def process_detail(name_quoted):
    proc_name = name_quoted.strip("'")
    with _LOCK:
        proc = next((p for p in _CATALOG["processes"] if p["name"] == proc_name), None)
        if not proc:
            return jsonify({"error": "not found"}), 404
        return jsonify({
            "Name": proc_name,
            "LastMessage": {
                "Status": proc["status"],
                "ExecutionID": proc["execution_id"],
            },
        })


@app.route("/api/v1/ExecuteMDX", methods=["POST"])
def execute_mdx():
    return jsonify({"ID": "cellset-mock-01", "@odata.id": "Cellsets('cellset-mock-01')"})


@app.route("/api/v1/Cellsets(<path:cs_quoted>)", methods=["GET"])
def cellset(cs_quoted):
    return jsonify({
        "ID": cs_quoted.strip("'"),
        "Cells": [
            {"Value": round(random.uniform(100, 1000), 2), "Ordinal": i}
            for i in range(6)
        ],
        "Axes": [
            {
                "Tuples": [
                    {"Members": [{"Name": "Jan", "Hierarchy": {"Dimension": {"Name": "Period"}}}]},
                    {"Members": [{"Name": "Feb", "Hierarchy": {"Dimension": {"Name": "Period"}}}]},
                    {"Members": [{"Name": "Mar", "Hierarchy": {"Dimension": {"Name": "Period"}}}]},
                ]
            },
            {
                "Tuples": [
                    {"Members": [{"Name": "ProductA", "Hierarchy": {"Dimension": {"Name": "Product"}}}]},
                    {"Members": [{"Name": "ProductB", "Hierarchy": {"Dimension": {"Name": "Product"}}}]},
                ]
            },
        ],
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5495)
PYEOF

cat > "$MOCK_DIR/Dockerfile" <<'DOCKERFILEEOF'
FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir flask==3.0.0
COPY mock_tm1.py .
EXPOSE 5495
CMD ["python", "mock_tm1.py"]
DOCKERFILEEOF

echo ">>> Building mock TM1 image ($MOCK_IMAGE)"
docker build -q -t "$MOCK_IMAGE" "$MOCK_DIR" >/dev/null

# --- 2. Start the mock ---------------------------------------------------
docker rm -f tm1-mock >/dev/null 2>&1 || true
echo ">>> Starting mock TM1 on port $TM1_PORT"
docker run -d --name tm1-mock -p "$TM1_PORT:5495" "$MOCK_IMAGE" >/dev/null

for i in $(seq 1 30); do
  if curl -fsS -m 2 "http://localhost:$TM1_PORT/api/v1/Configuration" >/dev/null 2>&1; then
    echo "    ✓ Mock TM1 ready after ${i}s"; break
  fi
  sleep 1
done

# --- 3. Scaffold a Dagster project --------------------------------------
echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
rm -rf "$PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"
uv add --dev -q dagster-dg-cli dagster-webserver

# --- 4. Install the five TM1 components ---------------------------------
echo ">>> Installing tm1 components"
CLI="uvx --from dagster-community-components-cli dagster-component"
$CLI --refresh add tm1_resource --auto-install
$CLI add tm1_process_trigger_job --auto-install
$CLI add tm1_process_status_sensor --auto-install
$CLI add tm1_cube_data_ingestion --auto-install
$CLI add tm1_workspace --auto-install

# --- 5. Wire defs.yaml files against the mock ----------------------------
echo ">>> Wiring defs.yaml files"

cat > "src/$PKG/defs/tm1_resource/defs.yaml" <<YAML
type: dagster_community_components.TM1ResourceComponent
attributes:
  resource_key: tm1_resource
  base_url_env_var: TM1_URL
  username_env_var: TM1_USER
  password_env_var: TM1_PASSWORD
  verify_ssl: false
YAML

cat > "src/$PKG/defs/tm1_process_trigger_job/defs.yaml" <<YAML
type: dagster_community_components.TM1ProcessTriggerJobComponent
attributes:
  job_name: run_gl_load
  target_type: process
  target_name: LoadActualsFromGL
  parameters:
    pYear: "2026"
    pMonth: "07"
  resource_key: tm1_resource
  wait_for_completion: true
  timeout_seconds: 60
YAML

cat > "src/$PKG/defs/tm1_process_status_sensor/defs.yaml" <<YAML
type: dagster_community_components.TM1ProcessStatusSensorComponent
attributes:
  sensor_name: gl_load_done
  process_name: LoadActualsFromGL
  target_statuses: [CompletedSuccessfully]
  job_name: run_gl_load
  resource_key: tm1_resource
  minimum_interval_seconds: 30
  default_status: stopped
YAML

cat > "src/$PKG/defs/tm1_cube_data_ingestion/defs.yaml" <<YAML
type: dagster_community_components.TM1CubeDataIngestionComponent
attributes:
  asset_key: sales_by_product_period
  cube: Sales
  row_dimensions: [Period]
  column_dimensions: [Product]
  group_name: tm1_actuals
  resource_key: tm1_resource
YAML

cat > "src/$PKG/defs/tm1_workspace/defs.yaml" <<YAML
type: dagster_community_components.TM1WorkspaceComponent
attributes:
  base_url_env_var: TM1_URL
  username_env_var: TM1_USER
  password_env_var: TM1_PASSWORD
  verify_ssl: false
  cube_selector:
    by_name: [Sales, Finance]
  process_selector:
    by_pattern: ["Load*", "Refresh*"]
  chore_selector:
    by_name: [DailyRebuild]
  group_name: tm1_workspace
  wait_for_completion: true
  timeout_seconds: 60
  defs_state:
    management_type: LOCAL_FILESYSTEM
    refresh_if_dev: true
YAML

# --- 6. Env vars ---------------------------------------------------------
export TM1_URL="http://localhost:$TM1_PORT"
export TM1_USER="demo"
export TM1_PASSWORD="demo"

cat > ".env" <<ENVEOF
TM1_URL=$TM1_URL
TM1_USER=$TM1_USER
TM1_PASSWORD=$TM1_PASSWORD
ENVEOF

# --- 7. Validate ---------------------------------------------------------
DCC="dagster-community-components @ https://github.com/eric-thomas-dagster/dagster-component-templates/archive/$COMMIT_SHA.zip"

echo ">>> Validating defs (dg check)"
uv run --with "$DCC" dg check defs || { echo "    ✗ dg check failed"; exit 1; }

echo ">>> Triggering LoadActualsFromGL"
uv run --with "$DCC" dg launch --job run_gl_load || { echo "    ✗ trigger job failed"; exit 1; }

echo ">>> Materializing sales_by_product_period"
uv run --with "$DCC" dg launch --assets sales_by_product_period || { echo "    ✗ cube ingestion failed"; exit 1; }

echo ">>> Refreshing workspace state"
uv run --with "$DCC" dg utils refresh-defs-state || { echo "    ✗ refresh failed"; exit 1; }

echo ">>> Materializing a workspace-emitted process asset"
uv run --with "$DCC" dg launch --assets 'tm1/process/LoadActualsFromGL' || { echo "    ✗ workspace asset failed"; exit 1; }

cat <<DONE

✓ TM1 demo up.

Mock TM1 server:  http://localhost:$TM1_PORT
Dagster project:  $(pwd)
Env vars:
  export TM1_URL="$TM1_URL"
  export TM1_USER="demo"
  export TM1_PASSWORD="demo"

Next:
  cd $PROJECT_DIR
  uv run --with "$DCC" dg dev
  # → http://localhost:3000

Cleanup:
  docker rm -f tm1-mock
  docker rmi $MOCK_IMAGE
DONE
