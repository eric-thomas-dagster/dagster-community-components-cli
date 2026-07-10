#!/usr/bin/env bash
# Cognos Analytics end-to-end demo — mock Cognos REST in Docker.
set -eo pipefail

PROJECT_DIR="${1:-cognos-demo}"
COGNOS_PORT="${COGNOS_PORT:-9300}"
MOCK_IMAGE="${MOCK_IMAGE:-cognos-mock:demo}"
COMMIT_SHA="692a8752"

if ! command -v docker >/dev/null 2>&1; then echo "✗ docker required"; exit 1; fi
if ! command -v uv >/dev/null 2>&1; then echo "✗ uv required"; exit 1; fi

MOCK_DIR="/tmp/cognos-mock-src"
rm -rf "$MOCK_DIR" && mkdir -p "$MOCK_DIR"

cat > "$MOCK_DIR/mock_cognos.py" <<'PYEOF'
"""Mock Cognos Analytics REST API."""
import random
import time
from flask import Flask, jsonify, request, Response

app = Flask(__name__)

_REPORTS = [
    {"id": "iABC001", "name": "MonthlyPnL"},
    {"id": "iABC002", "name": "DailySalesSummary"},
    {"id": "iABC003", "name": "HeadcountByDept"},
    {"id": "iABC004", "name": "LegacyReport_deprecated"},
]


@app.route("/api/v1/session", methods=["POST"])
def login():
    body = request.get_json(silent=True) or {}
    params = {p["name"]: p["value"] for p in body.get("parameters", [])}
    if not params.get("CAMUsername") or not params.get("CAMPassword"):
        return jsonify({"error": "creds required"}), 401
    return jsonify({"session": "mock-session-token"})


@app.route("/api/v1/content/items", methods=["GET"])
@app.route("/api/v1/content//items", methods=["GET"])
def content_root():
    if request.args.get("type") == "report":
        return jsonify({"data": [{"id": r["id"], "defaultName": r["name"]} for r in _REPORTS]})
    return jsonify({"data": []})


@app.route("/api/v1/reports/<report_id>/data", methods=["POST"])
def run_report(report_id):
    body = request.get_json(silent=True) or {}
    fmt = (body.get("format") or "CSV").upper()
    if fmt == "CSV":
        return Response(
            "row_id,account,amount,period\n1,Revenue,120000,2026-07\n2,COGS,45000,2026-07\n3,Gross Margin,75000,2026-07\n",
            mimetype="text/csv",
        )
    if fmt == "JSON":
        return jsonify({
            "data": [
                {"row_id": 1, "account": "Revenue", "amount": 120000, "period": "2026-07"},
                {"row_id": 2, "account": "COGS", "amount": 45000, "period": "2026-07"},
                {"row_id": 3, "account": "Gross Margin", "amount": 75000, "period": "2026-07"},
            ]
        })
    # PDF / HTML / XLSX / XML — return a stub bytes blob.
    return Response(b"MOCK-REPORT-OUTPUT-%s" % fmt.encode(), mimetype="application/octet-stream")


@app.route("/api/v1/reports/<report_id>/history", methods=["GET"])
def report_history(report_id):
    return jsonify({
        "history": [
            {"id": f"run-{report_id}-{int(time.time())}",
             "status": "COMPLETED",
             "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        ]
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9300)
PYEOF

cat > "$MOCK_DIR/Dockerfile" <<'DOCKERFILEEOF'
FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir flask==3.0.0
COPY mock_cognos.py .
EXPOSE 9300
CMD ["python", "mock_cognos.py"]
DOCKERFILEEOF

echo ">>> Building mock Cognos image ($MOCK_IMAGE)"
docker build -q -t "$MOCK_IMAGE" "$MOCK_DIR" >/dev/null

docker rm -f cognos-mock >/dev/null 2>&1 || true
echo ">>> Starting mock Cognos on port $COGNOS_PORT"
docker run -d --name cognos-mock -p "$COGNOS_PORT:9300" "$MOCK_IMAGE" >/dev/null

for i in $(seq 1 30); do
  if curl -fsS -m 2 -X POST "http://localhost:$COGNOS_PORT/api/v1/session" \
      -H "Content-Type: application/json" \
      -d '{"parameters":[{"name":"CAMNamespace","value":"LDAP"},{"name":"CAMUsername","value":"demo"},{"name":"CAMPassword","value":"demo"}]}' >/dev/null 2>&1; then
    echo "    ✓ Mock Cognos ready after ${i}s"; break
  fi
  sleep 1
done

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
rm -rf "$PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"
uv add --dev -q dagster-dg-cli dagster-webserver

echo ">>> Installing cognos components"
CLI="uvx --from dagster-community-components-cli dagster-component"
$CLI --refresh add cognos_resource --auto-install
$CLI add cognos_report_run_job --auto-install
$CLI add cognos_report_status_sensor --auto-install
$CLI add cognos_report_data_ingestion --auto-install
$CLI add cognos_workspace --auto-install

echo ">>> Wiring defs.yaml files"

cat > "src/$PKG/defs/cognos_resource/defs.yaml" <<YAML
type: dagster_community_components.CognosResourceComponent
attributes:
  resource_key: cognos_resource
  base_url_env_var: COGNOS_URL
  username_env_var: COGNOS_USER
  password_env_var: COGNOS_PASSWORD
  namespace_env_var: COGNOS_NAMESPACE
  verify_ssl: false
YAML

cat > "src/$PKG/defs/cognos_report_run_job/defs.yaml" <<YAML
type: dagster_community_components.CognosReportRunJobComponent
attributes:
  job_name: run_monthly_pnl
  report_id: iABC001
  output_format: CSV
  resource_key: cognos_resource
  wait_for_completion: false           # mock returns synchronously — no async job
  timeout_seconds: 60
YAML

cat > "src/$PKG/defs/cognos_report_status_sensor/defs.yaml" <<YAML
type: dagster_community_components.CognosReportStatusSensorComponent
attributes:
  sensor_name: monthly_pnl_done
  report_id: iABC001
  target_statuses: [COMPLETED]
  job_name: run_monthly_pnl
  resource_key: cognos_resource
  minimum_interval_seconds: 30
  default_status: stopped
YAML

cat > "src/$PKG/defs/cognos_report_data_ingestion/defs.yaml" <<YAML
type: dagster_community_components.CognosReportDataIngestionComponent
attributes:
  asset_key: monthly_pnl_data
  report_id: iABC001
  output_format: CSV
  group_name: cognos_finance
  resource_key: cognos_resource
YAML

cat > "src/$PKG/defs/cognos_workspace/defs.yaml" <<YAML
type: dagster_community_components.CognosWorkspaceComponent
attributes:
  base_url_env_var: COGNOS_URL
  username_env_var: COGNOS_USER
  password_env_var: COGNOS_PASSWORD
  namespace_env_var: COGNOS_NAMESPACE
  verify_ssl: false
  report_selector:
    by_pattern: ["Monthly*", "Daily*"]
    exclude_by_pattern: ["*_deprecated"]
  group_name: cognos_workspace
  output_format: CSV
  wait_for_completion: false
  timeout_seconds: 60
  defs_state:
    management_type: LOCAL_FILESYSTEM
    refresh_if_dev: true
YAML

export COGNOS_URL="http://localhost:$COGNOS_PORT"
export COGNOS_USER="demo"
export COGNOS_PASSWORD="demo"
export COGNOS_NAMESPACE="LDAP"

cat > ".env" <<ENVEOF
COGNOS_URL=$COGNOS_URL
COGNOS_USER=$COGNOS_USER
COGNOS_PASSWORD=$COGNOS_PASSWORD
COGNOS_NAMESPACE=$COGNOS_NAMESPACE
ENVEOF

DCC="dagster-community-components @ https://github.com/eric-thomas-dagster/dagster-component-templates/archive/$COMMIT_SHA.zip"

echo ">>> Validating defs (dg check)"
uv run --with "$DCC" dg check defs || { echo "    ✗ dg check failed"; exit 1; }

echo ">>> Triggering run_monthly_pnl"
uv run --with "$DCC" dg launch --job run_monthly_pnl || { echo "    ✗ trigger failed"; exit 1; }

echo ">>> Materializing monthly_pnl_data (report → CSV → DataFrame)"
uv run --with "$DCC" dg launch --assets monthly_pnl_data || { echo "    ✗ ingestion failed"; exit 1; }

echo ">>> Refreshing workspace state"
uv run --with "$DCC" dg utils refresh-defs-state || { echo "    ✗ refresh failed"; exit 1; }

echo ">>> Materializing a workspace-emitted report asset"
uv run --with "$DCC" dg launch --assets 'cognos/report/MonthlyPnL' || { echo "    ✗ workspace asset failed"; exit 1; }

cat <<DONE

✓ Cognos demo up.

Mock Cognos:        http://localhost:$COGNOS_PORT
Dagster project:    $(pwd)

Cleanup:
  docker rm -f cognos-mock
  docker rmi $MOCK_IMAGE
DONE
