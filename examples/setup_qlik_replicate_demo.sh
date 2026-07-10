#!/usr/bin/env bash
# Qlik Replicate end-to-end demo — mock Enterprise Manager in Docker, live-validated.
#
# WHAT THIS DEMONSTRATES
#   The four Qlik Replicate components shipped in the community registry:
#
#     1. qlik_replicate_resource                 — shared EM auth
#     2. qlik_replicate_task_trigger_job         — start / stop / reload a task
#     3. qlik_replicate_task_status_sensor       — event-drive on task state
#     4. qlik_replicate_task_metrics_ingestion   — per-task CDC metrics DataFrame
#
#   The demo spins up a mock Qlik Enterprise Manager (Python + Flask in a
#   container) that responds to the same REST endpoints as a real EM,
#   scaffolds a Dagster project, wires all four components against the mock,
#   and materializes to prove end-to-end wiring. The mock stubs task states
#   deterministically so demos are repeatable.
#
# COST: $0 — mock in Docker. Pulls python:3.11-slim (~130 MB) on first run.

set -eo pipefail

PROJECT_DIR="${1:-qlik-replicate-demo}"
QLIK_EM_PORT="${QLIK_EM_PORT:-4442}"
MOCK_IMAGE="${MOCK_IMAGE:-qlik-em-mock:demo}"

# --- 0. Tool check -------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  echo "✗ docker required"; exit 1
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "✗ uv required"; exit 1
fi

# --- 1. Build the mock Qlik Enterprise Manager -----------------------------
# Speaks the same REST shape as the real EM: /login, /servers/<s>/tasks,
# /servers/<s>/tasks/<t> (GET + POST with ?action= param). State is
# in-memory: tasks start STOPPED; run/reload move them to RUNNING then
# STOPPED after ~5s. Metrics are pseudo-random so the ingestion asset
# shows different values on each poll.
MOCK_DIR="/tmp/qlik-em-mock-src"
rm -rf "$MOCK_DIR" && mkdir -p "$MOCK_DIR"

cat > "$MOCK_DIR/mock_em.py" <<'PYEOF'
"""Mock Qlik Enterprise Manager REST API for the qlik_replicate demo."""
import random
import time
import threading

from flask import Flask, jsonify, request

app = Flask(__name__)

_STATE_LOCK = threading.Lock()
_TASKS = {
    "prod-replicate-01": {
        "orders_sqlserver_to_snowflake": {
            "state": "RUNNING",
            "stage": "CDC",
            "start_time": time.time() - 3600,
            "last_state_change_time": "2026-07-10T00:00:00Z",
            "cdc_latency_seconds": 3.2,
            "error_count": 0,
        },
        "customers_db2_to_snowflake": {
            "state": "STOPPED",
            "stage": "FULL_LOAD",
            "start_time": None,
            "last_state_change_time": "2026-07-09T22:00:00Z",
            "cdc_latency_seconds": None,
            "error_count": 0,
        },
    }
}


def _apply_action(task, action, option):
    """Mutate task state deterministically for demo purposes."""
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if action == "run":
        task["state"] = "STARTING"
        task["stage"] = "FULL_LOAD" if option == "RELOAD_TARGET" else "CDC"
        task["start_time"] = time.time()
        task["last_state_change_time"] = now_iso

        def _promote():
            time.sleep(2)
            with _STATE_LOCK:
                task["state"] = "RUNNING"
                task["last_state_change_time"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        threading.Thread(target=_promote, daemon=True).start()
    elif action == "stop":
        task["state"] = "STOPPED"
        task["last_state_change_time"] = now_iso
    elif action == "reload":
        task["state"] = "STARTING"
        task["stage"] = "FULL_LOAD"
        task["start_time"] = time.time()
        task["last_state_change_time"] = now_iso

        def _promote():
            time.sleep(1)
            with _STATE_LOCK:
                task["state"] = "RUNNING"
            time.sleep(3)
            with _STATE_LOCK:
                task["state"] = "STOPPED"
                task["stage"] = "CDC"
                task["last_state_change_time"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        threading.Thread(target=_promote, daemon=True).start()
    else:
        raise ValueError(f"unknown action {action!r}")


@app.route("/attunityenterprisemanager/api/v1/login", methods=["POST"])
def login():
    body = request.get_json(silent=True) or {}
    if not body.get("username") or not body.get("password"):
        return jsonify({"error": "credentials required"}), 401
    return jsonify({"sessionId": "mock-session-id", "user": body["username"]})


@app.route("/attunityenterprisemanager/api/v1/servers", methods=["GET"])
def list_servers():
    return jsonify({"serverList": [{"name": s} for s in _TASKS.keys()]})


@app.route("/attunityenterprisemanager/api/v1/servers/<server>/tasks", methods=["GET"])
def list_tasks(server):
    with _STATE_LOCK:
        tasks = _TASKS.get(server, {})
        return jsonify({"taskList": [{"name": t} for t in tasks.keys()]})


@app.route("/attunityenterprisemanager/api/v1/servers/<server>/tasks/<task>", methods=["GET", "POST"])
def task_detail(server, task):
    with _STATE_LOCK:
        if server not in _TASKS or task not in _TASKS[server]:
            return jsonify({"error": f"unknown {server}/{task}"}), 404

        if request.method == "POST":
            action = request.args.get("action", "").lower()
            option = request.args.get("option", "")
            _apply_action(_TASKS[server][task], action, option)
            return jsonify({"ok": True, "action": action})

        # Return detail with pseudo-random metrics so ingestion sees change.
        t = _TASKS[server][task]
        return jsonify({
            "task": {
                "name": task,
                "state": t["state"],
                "stage": t["stage"],
                "start_time": t["start_time"],
                "last_state_change_time": t["last_state_change_time"],
                "cdc_latency_seconds": t["cdc_latency_seconds"] if t["cdc_latency_seconds"] is not None else round(random.uniform(0.5, 10.0), 2),
                "error_count": t["error_count"],
                "cdc_event_counters": {
                    "source_commits": random.randint(100, 10000),
                    "target_commits": random.randint(100, 10000),
                    "source_records": random.randint(1000, 100000),
                    "target_records": random.randint(1000, 100000),
                    "source_latency": t["cdc_latency_seconds"] if t["cdc_latency_seconds"] is not None else round(random.uniform(0.5, 10.0), 2),
                    "errors": t["error_count"],
                },
                "full_load_counters": {
                    "completed_percent": 100.0 if t["stage"] == "CDC" else round(random.uniform(0, 100), 1),
                    "source_records": random.randint(1000, 100000),
                    "target_records": random.randint(1000, 100000),
                },
            }
        })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=4442)
PYEOF

cat > "$MOCK_DIR/Dockerfile" <<'DOCKERFILEEOF'
FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir flask==3.0.0
COPY mock_em.py .
EXPOSE 4442
CMD ["python", "mock_em.py"]
DOCKERFILEEOF

echo ">>> Building mock Qlik Enterprise Manager image ($MOCK_IMAGE)"
docker build -q -t "$MOCK_IMAGE" "$MOCK_DIR" >/dev/null

# --- 2. Start the mock -----------------------------------------------------
docker rm -f qlik-em-mock >/dev/null 2>&1 || true
echo ">>> Starting mock Enterprise Manager on port $QLIK_EM_PORT"
docker run -d --name qlik-em-mock -p "$QLIK_EM_PORT:4442" "$MOCK_IMAGE" >/dev/null

# Wait until the mock responds.
for i in $(seq 1 30); do
  if curl -fsS -m 2 -X POST "http://localhost:$QLIK_EM_PORT/attunityenterprisemanager/api/v1/login" \
      -H "Content-Type: application/json" \
      -d '{"username":"demo","password":"demo"}' >/dev/null 2>&1; then
    echo "    ✓ Mock EM ready after ${i}s"
    break
  fi
  sleep 1
done

# --- 3. Scaffold a Dagster project ----------------------------------------
echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
rm -rf "$PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"
uv add --dev -q dagster-dg-cli dagster-webserver

# --- 4. Install the five Qlik Replicate components -----------------------
echo ">>> Installing qlik_replicate components"
CLI="uvx --from dagster-community-components-cli dagster-component"
$CLI --refresh add qlik_replicate_resource --auto-install
$CLI add qlik_replicate_task_trigger_job --auto-install
$CLI add qlik_replicate_task_status_sensor --auto-install
$CLI add qlik_replicate_task_metrics_ingestion --auto-install
$CLI add qlik_replicate_workspace --auto-install

# --- 5. Configure defs.yaml for each component ---------------------------
echo ">>> Wiring defs.yaml files against the mock EM"

# Resource — points at the mock. Uses basic auth (mock accepts any user/pass).
cat > "src/$PKG/defs/qlik_replicate_resource/defs.yaml" <<YAML
type: dagster_community_components.QlikReplicateResourceComponent
attributes:
  resource_key: qlik_replicate_resource
  base_url_env_var: QLIK_EM_URL
  username_env_var: QLIK_EM_USER
  password_env_var: QLIK_EM_PASSWORD
  verify_ssl: false
YAML

# Trigger — reload the orders task, wait for completion.
cat > "src/$PKG/defs/qlik_replicate_task_trigger_job/defs.yaml" <<YAML
type: dagster_community_components.QlikReplicateTaskTriggerJobComponent
attributes:
  job_name: reload_orders_cdc
  server: prod-replicate-01
  task: orders_sqlserver_to_snowflake
  action: reload
  wait_for_completion: true
  poll_interval_seconds: 2
  timeout_seconds: 60
  resource_key: qlik_replicate_resource
YAML

# Sensor — fires when orders task hits STOPPED (which it will after the reload).
cat > "src/$PKG/defs/qlik_replicate_task_status_sensor/defs.yaml" <<YAML
type: dagster_community_components.QlikReplicateTaskStatusSensorComponent
attributes:
  sensor_name: orders_reload_done
  server: prod-replicate-01
  task: orders_sqlserver_to_snowflake
  target_states: [STOPPED]
  job_name: reload_orders_cdc      # re-run reload for demo purposes
  resource_key: qlik_replicate_resource
  minimum_interval_seconds: 30
  default_status: stopped
YAML

# Metrics ingestion — DataFrame of per-task metrics
cat > "src/$PKG/defs/qlik_replicate_task_metrics_ingestion/defs.yaml" <<YAML
type: dagster_community_components.QlikReplicateTaskMetricsIngestionComponent
attributes:
  asset_key: qlik_task_metrics
  servers: [prod-replicate-01]
  group_name: qlik_observability
  resource_key: qlik_replicate_resource
YAML

# Workspace — StateBackedComponent, auto-emits one asset per task.
cat > "src/$PKG/defs/qlik_replicate_workspace/defs.yaml" <<YAML
type: dagster_community_components.QlikReplicateWorkspaceComponent
attributes:
  base_url_env_var: QLIK_EM_URL
  username_env_var: QLIK_EM_USER
  password_env_var: QLIK_EM_PASSWORD
  verify_ssl: false
  servers: [prod-replicate-01]
  task_selector:
    by_name: [orders_sqlserver_to_snowflake, customers_db2_to_snowflake]
  group_name: qlik_workspace
  action: reload
  wait_for_completion: true
  poll_interval_seconds: 2
  timeout_seconds: 60
  defs_state:
    management_type: LOCAL_FILESYSTEM
    refresh_if_dev: true
YAML

# --- 6. Wire env vars into pyproject.toml ---------------------------------
# Dagster runs on the host (not inside a container), and the mock's port is
# host-published, so localhost is correct on both macOS and Linux.
export QLIK_EM_URL="http://localhost:$QLIK_EM_PORT"
export QLIK_EM_USER="demo"
export QLIK_EM_PASSWORD="demo"

cat > ".env" <<ENVEOF
QLIK_EM_URL=$QLIK_EM_URL
QLIK_EM_USER=$QLIK_EM_USER
QLIK_EM_PASSWORD=$QLIK_EM_PASSWORD
ENVEOF

# --- 7. Validate: dg check + trigger the job + materialize metrics -------
echo ">>> Validating defs (dg check)"
uv run --with "dagster-community-components @ https://github.com/eric-thomas-dagster/dagster-component-templates/archive/c5b8f4e4.zip" dg check defs || {
  echo "    ✗ dg check failed"
  exit 1
}

echo ">>> Running the reload_orders_cdc job (mock will move STARTING → RUNNING → STOPPED)"
uv run --with "dagster-community-components @ https://github.com/eric-thomas-dagster/dagster-component-templates/archive/c5b8f4e4.zip" dg launch --job reload_orders_cdc || {
  echo "    ✗ trigger job failed"
  exit 1
}

echo ">>> Materializing qlik_task_metrics"
uv run --with "dagster-community-components @ https://github.com/eric-thomas-dagster/dagster-component-templates/archive/c5b8f4e4.zip" dg launch --assets qlik_task_metrics || {
  echo "    ✗ metrics materialization failed"
  exit 1
}

echo ">>> Refreshing workspace state (StateBackedComponent write_state_to_path)"
uv run --with "dagster-community-components @ https://github.com/eric-thomas-dagster/dagster-component-templates/archive/c5b8f4e4.zip" dg utils refresh-defs-state || {
  echo "    ✗ refresh-defs-state failed"
  exit 1
}

echo ">>> Materializing a workspace-emitted asset (per-task asset auto-generated)"
uv run --with "dagster-community-components @ https://github.com/eric-thomas-dagster/dagster-component-templates/archive/c5b8f4e4.zip" dg launch --assets 'qlik_replicate/prod-replicate-01/orders_sqlserver_to_snowflake' || {
  echo "    ✗ workspace asset materialization failed"
  exit 1
}

# --- 8. Done -----------------------------------------------------------
cat <<DONE

✓ Qlik Replicate demo up.

Mock Enterprise Manager: http://localhost:$QLIK_EM_PORT
Dagster project:         $(pwd)
Env vars to load in this shell:
  export QLIK_EM_URL="$QLIK_EM_URL"
  export QLIK_EM_USER="demo"
  export QLIK_EM_PASSWORD="demo"

Next:
  cd $PROJECT_DIR
  uv run --with "dagster-community-components @ https://github.com/eric-thomas-dagster/dagster-component-templates/archive/c5b8f4e4.zip" dg dev
  # → http://localhost:3000
  # → click "Materialize" on qlik_task_metrics
  # → toggle orders_reload_done sensor ON

Cleanup:
  docker rm -f qlik-em-mock
  docker rmi $MOCK_IMAGE
DONE
