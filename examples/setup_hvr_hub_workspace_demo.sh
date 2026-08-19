#!/usr/bin/env bash
# HVR Hub Workspace demo — mock HVR Hub REST server + Dagster catalog.
#
# WHY MOCK, NOT DOCKER?
#   Fivetran ships a `fivetraninc/hvrpov` Docker eval image, but creating
#   a hub against it requires a temporary license key from Fivetran Support.
#   The mock server exposes exactly the 5 endpoints the component uses,
#   with realistic responses (auth → channels → tables → loc_groups → jobs
#   w/ latency). Ships in a Python venv, ~100 lines, zero external
#   infrastructure. Full end-to-end validation of the Dagster surface.
#
# WHAT THIS DEMONSTRATES
#   `hvr_hub_workspace` (StateBackedComponent, full Fivetran-shape) picks
#   up 4 replicated tables + 1 polling sensor + 4 asset checks in one YAML.
#   Same YAML retargets at your real HVR Hub by swapping 4 env vars.
#
# TO POINT AT A REAL HVR HUB
#   1. Skip step 1 (mock server); leave your real Hub running.
#   2. Change the 4 env vars in .env.demo to point at your Hub.
#   3. Re-run `uv run dg utils refresh-defs-state && uv run dg dev`.

set -euo pipefail

PROJECT_DIR="${1:-hvr-hub-demo}"
MOCK_PORT=4340
MOCK_PID_FILE=/tmp/hvr_mock_hub.pid

ok()   { printf '\033[32m✓\033[0m %s\n' "$*"; }
info() { printf '\033[36m>>>\033[0m %s\n' "$*"; }
die()  { printf '\033[31m✗\033[0m %s\n' "$*"; exit 1; }

# ── 1. Start the mock HVR Hub REST server ────────────────────────────────

info "1/5  Starting mock HVR Hub REST server on port $MOCK_PORT"
if [ -f "$MOCK_PID_FILE" ] && kill -0 "$(cat "$MOCK_PID_FILE")" 2>/dev/null; then
  kill "$(cat "$MOCK_PID_FILE")" 2>/dev/null || true
  sleep 1
fi

MOCK_SRC=/tmp/hvr_mock_hub.py
cat > "$MOCK_SRC" <<'PY'
"""Mock HVR Hub 6.x REST server — 5 endpoints the workspace component hits.
Realistic responses; matches shapes the real Hub returns."""
import random, time
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

app = FastAPI()

CHANNELS = {
    "sales_cdc":     {"tables": ["orders", "customers"]},
    "orders_stream": {"tables": ["events", "clicks"]},
    "inventory_cdc": {"tables": ["stock_levels"]},
    "test_ephemeral":{"tables": ["scratch"]},
}
LOC_GROUPS = {"SRC": {"role": "source"}, "target_snowflake_dw": {"role": "target"}}

@app.get("/api")
def api_versions():
    return ["v6.1.5.2", "v6.2.5", "v6.3.5", "latest"]

@app.post("/auth/v1/password")
def login(body: dict):
    if body.get("username") and body.get("password"):
        return {"access_token": "mock-jwt-token", "token_type": "bearer", "expires_in": 3600}
    raise HTTPException(401, "F_JW054F: Authentication required.")

@app.get("/api/{ver}/hubs/{hub}/definition/channels")
def list_channels(ver: str, hub: str, authorization: str = Header(...)):
    return {name: {"description": f"channel {name}"} for name in CHANNELS}

@app.get("/api/{ver}/hubs/{hub}/definition/channels/{channel}/tables")
def list_tables(ver: str, hub: str, channel: str, authorization: str = Header(...)):
    tables = CHANNELS.get(channel, {}).get("tables", [])
    return {t: {"base_name": t, "schema": "public"} for t in tables}

@app.get("/api/{ver}/hubs/{hub}/definition/channels/{channel}/loc_groups")
def list_loc_groups(ver: str, hub: str, channel: str, authorization: str = Header(...)):
    return LOC_GROUPS

@app.get("/api/{ver}/hubs/{hub}/jobs")
def list_jobs(ver: str, hub: str, fetch: str = "", authorization: str = Header(...)):
    # Realistic per-channel integrate lag — random between 5s and 45s so the
    # SLA check has something to react to.
    jobs = {}
    for ch, meta in CHANNELS.items():
        job_name = f"{ch}-integ-target_snowflake_dw"
        jobs[job_name] = {"latency": random.uniform(5, 45), "state": "RUNNING"}
    return jobs

@app.post("/api/{ver}/hubs/{hub}/channels/{channel}/refresh")
def trigger_refresh(ver: str, hub: str, channel: str, authorization: str = Header(...)):
    return {"channel": channel, "status": "refresh_started", "started_at": time.time()}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=4340, log_level="error")
PY

# Isolated venv for the mock so we don't pollute the demo project.
python3 -m venv /tmp/hvr_mock_venv 2>/dev/null || true
# shellcheck disable=SC1091
source /tmp/hvr_mock_venv/bin/activate
pip install -q fastapi uvicorn >/dev/null 2>&1
nohup python "$MOCK_SRC" >/tmp/hvr_mock_hub.log 2>&1 &
echo $! > "$MOCK_PID_FILE"
deactivate

# Wait for the mock to accept traffic.
for i in $(seq 1 15); do
  curl -s "http://localhost:$MOCK_PORT/api" >/dev/null 2>&1 && break
  sleep 0.5
done
curl -s "http://localhost:$MOCK_PORT/api" | head -c 60
echo
ok "mock HVR Hub live on http://localhost:$MOCK_PORT   (log: /tmp/hvr_mock_hub.log)"

# ── 2. Scaffold Dagster project ──────────────────────────────────────────

info "2/5  Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q 'yarl<1.24' >/dev/null   # portability workaround
uv add -q 'dagster-community-components>=0.10.75' 'requests>=2.28' >/dev/null
uv add --dev -q dagster-dg-cli dagster-webserver >/dev/null

CLI="uvx --from dagster-community-components-cli dagster-component"

# ── 3. Install hvr_hub_workspace component ───────────────────────────────

info "3/5  Installing hvr_hub_workspace component"
# --refresh busts any stale local manifest cache from a prior CLI invocation
# (the component is fresh; a cache from before it shipped will 404).
$CLI --refresh search hvr_hub_workspace >/dev/null 2>&1 || true
# --as-package: install as a stub defs.yaml pointing at the PyPI-installed
# dagster_community_components.HvrHubWorkspaceComponent (no file copy). We
# overwrite the CLI's stub defs.yaml with our own in the next step.
$CLI add hvr_hub_workspace --as-package --auto-install 2>&1 | tail -3
ok "component installed"

# ── 4. Write defs.yaml + env ─────────────────────────────────────────────

mkdir -p "src/$PKG/defs/hvr_hub"
cat > "src/$PKG/defs/hvr_hub/defs.yaml" <<'YAML'
type: dagster_community_components.HvrHubWorkspaceComponent
attributes:
  workspace:
    hub_url:  "{{ env.HVR_HUB_URL }}"
    hub_name: "{{ env.HVR_HUB_NAME }}"
    username: "{{ env.HVR_USERNAME }}"
    password: "{{ env.HVR_PASSWORD }}"
    api_version: "latest"
    verify_ssl: false

  # Skip test_* channels — demonstrates channel_selector.
  channel_selector:
    exclude_by_pattern: [test_*]

  group_name: hvr
  action: noop                          # HVR CDC is continuous; materialization is a no-op
  polling_sensor: true
  observation_interval_seconds: 60
  freshness_lag_threshold_seconds: 30   # low bar so the check fails often — demonstrates alerting
YAML
ok "defs.yaml written"

cat > .env.demo <<EOF
export HVR_HUB_URL=http://localhost:$MOCK_PORT
export HVR_HUB_NAME=demo_hub
export HVR_USERNAME=hvradmin
export HVR_PASSWORD=DagsterDemo1
EOF
ok "wrote .env.demo"

# ── 5. Validate discovery ────────────────────────────────────────────────

info "5/5  Validating discovery against the mock Hub"
# shellcheck disable=SC1091
source .env.demo

uv run dg utils refresh-defs-state 2>&1 | grep -Ev "UserWarning|shadows|^warning:" | tail -3 || true
echo
uv run dg list defs 2>&1 | grep -Ev "UserWarning|shadows|^warning:" | grep -E "hvr/|_hvr_observer|integrate_lag" | head -20
echo

echo "═══════════════════════════════════════════════════════════════════════"
echo " HVR Hub Workspace demo — READY"
echo "═══════════════════════════════════════════════════════════════════════"
echo
echo "Mock Hub:       http://localhost:$MOCK_PORT   (log tail: /tmp/hvr_mock_hub.log)"
echo "Mock channels:  sales_cdc / orders_stream / inventory_cdc  (test_ephemeral filtered)"
echo
echo "Dagster surface:"
echo "  cd $PROJECT_DIR && source .env.demo"
echo "  uv run dg dev                  # UI at http://localhost:3000"
echo
echo "You'll see:"
echo "  - 5 external assets in the 'hvr' group (orders, customers, events, clicks, stock_levels)"
echo "  - Sensor 'demo_hub_hvr_observer' polling integrate-lag every 60s"
echo "  - Multi-asset check 'integrate_lag_within_sla' (fails often — 30s SLA is tight)"
echo
echo "Retarget at your real Hub — change ONLY the 4 env vars in .env.demo:"
echo "  HVR_HUB_URL=https://your-hub:4340"
echo "  HVR_HUB_NAME=prod_hub"
echo "  HVR_USERNAME=..."
echo "  HVR_PASSWORD=..."
echo "Everything else is identical."
echo
echo "Cleanup:  kill \$(cat $MOCK_PID_FILE)  &&  rm -rf $PROJECT_DIR /tmp/hvr_mock_*"
