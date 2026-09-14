#!/usr/bin/env bash
# RunMyJobs Integration end-to-end demo — Dagster wraps Redwood RunMyJobs
# (aka SAP Redwood Scheduler) JobDefinitions as daily-partitioned assets,
# with ops for restart/hold/release/kill/reconcile, and a RMJ ↔ Dagster
# inbound-trigger sensor.
#
# WHY MOCK, NOT DOCKER?
#   Redwood's PoC docker images are license-gated (require a temporary key
#   from Redwood Support). The mock server exposes exactly the 9 endpoints
#   the component hits — with realistic state machine transitions
#   (Waiting Time → Ready → Running → Completed), HTTP Basic Auth, and
#   per-process stdout/events. ~150 lines of FastAPI, no license, ships
#   in an isolated Python venv inside the project dir. Full end-to-end
#   validation of the REAL _execute_runmyjobs code path — not the
#   in-process stdout simulator.
#
# WHAT THIS DEMONSTRATES
#   `runmyjobs_integration` — each declared JobDefinition becomes a
#   daily-partitioned Dagster asset. Materialization submits to the mock
#   RMJ via REST, polls the process to terminal state (Completed), pulls
#   stdout, posts a completion event.
#
# TO POINT AT A REAL RUNMYJOBS INSTANCE
#   1. Skip the mock (kill it or ignore).
#   2. Change RUNMYJOBS_ENDPOINT / RUNMYJOBS_USER / RUNMYJOBS_PASSWORD in
#      .env.demo to point at your instance.
#   3. Re-run `uv run dg launch --assets 'rmj_eod_settlement' --partition <date>`.
#
# COST: $0 — mock server + Dagster ephemeral instance.

set -euo pipefail

PROJECT_DIR="${1:-runmyjobs-demo}"
MOCK_PORT="${MOCK_PORT:-8890}"

ok()   { printf '\033[32m✓\033[0m %s\n' "$*"; }
info() { printf '\033[36m>>>\033[0m %s\n' "$*"; }

# ── 1. Scaffold Dagster project ──────────────────────────────────────────

info "1/5  Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"
PKG="$(ls src/ | head -1)"

uv add -q requests
uv add --dev -q dagster-dg-cli dagster-webserver

# ── 2. Build the mock RunMyJobs REST server ──────────────────────────────

info "2/5  Building mock RunMyJobs REST server (port $MOCK_PORT, inside project dir)"

MOCK_DIR="$PROJECT_ABS/rmj-mock"
MOCK_SRC="$MOCK_DIR/mock_rmj.py"
MOCK_PID_FILE="$MOCK_DIR/mock.pid"
MOCK_LOG="$MOCK_DIR/mock.log"
MOCK_VENV="$MOCK_DIR/venv"

# Kill any prior mock instance from a re-run.
if [ -f "$MOCK_PID_FILE" ] && kill -0 "$(cat "$MOCK_PID_FILE")" 2>/dev/null; then
  kill "$(cat "$MOCK_PID_FILE")" 2>/dev/null || true
  sleep 1
fi

mkdir -p "$MOCK_DIR"

cat > "$MOCK_SRC" <<'PY'
"""Mock RunMyJobs REST server — 9 endpoints the runmyjobs_integration
component hits. HTTP Basic Auth. Realistic state machine (Waiting Time ->
Ready -> Running -> Completed) that advances on each poll.
"""
import base64
import os
import time
import uuid
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import Response

app = FastAPI()

USER = os.environ.get("MOCK_USER", "svc_dagster")
PASSWORD = os.environ.get("MOCK_PASSWORD", "DagsterDemo1")


def _check_auth(auth_header: Optional[str]) -> None:
    if not auth_header or not auth_header.startswith("Basic "):
        raise HTTPException(401, "HTTP Basic Authentication required.")
    try:
        decoded = base64.b64decode(auth_header[len("Basic "):]).decode()
        user, pw = decoded.split(":", 1)
    except Exception:
        raise HTTPException(401, "Malformed Basic auth header.") from None
    if user != USER or pw != PASSWORD:
        raise HTTPException(401, f"Invalid credentials for user {user!r}.")


# State: processId -> {status, poll_count, job_definition, scheduled_time, application, queue, submitted_at}
PROCESSES: dict = {}
HELD_APPS: set = set()

# Deterministic state machine: 4 polls before Completed
POLL_TRANSITIONS = ["Waiting Time", "Ready", "Running", "Running", "Completed"]


@app.post("/scheduler/api/submitjob")
def submitjob(body: dict, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    process_id = f"RMJ-{uuid.uuid4().hex[:8].upper()}"
    application = body.get("application", "")
    if application in HELD_APPS:
        return {"processId": process_id, "status": "Held", "reason": f"Application {application!r} on hold"}
    PROCESSES[process_id] = {
        "processId": process_id,
        "jobDefinition": body.get("jobDefinition", ""),
        "application": application,
        "queue": body.get("queue", ""),
        "scheduledTime": body.get("scheduledTime", ""),
        "submittedAt": time.time(),
        "poll_count": 0,
        "status": POLL_TRANSITIONS[0],
    }
    return {"processId": process_id, "status": "Submitted"}


@app.get("/scheduler/api/processes/{process_id}")
def get_process(process_id: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    p = PROCESSES.get(process_id)
    if not p:
        raise HTTPException(404, f"Process {process_id!r} not found")
    idx = min(p["poll_count"], len(POLL_TRANSITIONS) - 1)
    p["status"] = POLL_TRANSITIONS[idx]
    p["poll_count"] += 1
    return {**p, "status": p["status"]}


@app.get("/scheduler/api/processes/{process_id}/stdout")
def process_stdout(process_id: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    p = PROCESSES.get(process_id)
    if not p:
        raise HTTPException(404, f"Process {process_id!r} not found")
    body = "\n".join([
        f"[MOCK RMJ] Process {process_id} started",
        f"[MOCK RMJ] JobDefinition: {p['jobDefinition']}",
        f"[MOCK RMJ] Application: {p['application']} / Queue: {p['queue']}",
        f"[MOCK RMJ] scheduledTime: {p['scheduledTime']}",
        f"[MOCK RMJ] Rows processed: 12345",
        f"[MOCK RMJ] Duration: {round(time.time() - p['submittedAt'], 2)}s",
        f"[MOCK RMJ] Process {process_id} completed OK",
    ])
    return Response(content=body, media_type="text/plain")


@app.post("/scheduler/api/processes/{process_id}/events")
def post_event(process_id: str, body: dict, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    if process_id not in PROCESSES:
        raise HTTPException(404, f"Process {process_id!r} not found")
    return {"processId": process_id, "eventReceived": True, "eventName": body.get("eventName", "")}


@app.post("/scheduler/api/processes/{process_id}/rerun")
def rerun(process_id: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    if process_id not in PROCESSES:
        raise HTTPException(404, f"Process {process_id!r} not found")
    p = PROCESSES[process_id]
    p["poll_count"] = 0
    p["status"] = POLL_TRANSITIONS[0]
    p["submittedAt"] = time.time()
    return {"processId": process_id, "action": "rerun"}


@app.post("/scheduler/api/processes/{process_id}/kill")
def kill(process_id: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    if process_id not in PROCESSES:
        raise HTTPException(404, f"Process {process_id!r} not found")
    PROCESSES[process_id]["status"] = "Killed"
    return {"processId": process_id, "action": "kill"}


@app.post("/scheduler/api/applications/{app}/hold")
def hold(app: str, body: dict, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    HELD_APPS.add(app)
    return {"application": app, "held": True, "queue": body.get("queue", "")}


@app.post("/scheduler/api/applications/{app}/release")
def release(app: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    HELD_APPS.discard(app)
    return {"application": app, "held": False}


@app.get("/scheduler/api/processes")
def list_processes(since: str = "1h", limit: int = 200, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    processes = list(PROCESSES.values())[:limit]
    return {"processes": processes, "count": len(processes)}


if __name__ == "__main__":
    import sys
    import uvicorn
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8890
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")
PY

# Isolated venv inside the project dir — no /tmp pollution, no Windows portability trap.
python3 -m venv "$MOCK_VENV"
"$MOCK_VENV/bin/pip" install -q fastapi uvicorn >/dev/null 2>&1
MOCK_USER=svc_dagster MOCK_PASSWORD=DagsterDemo1 \
  nohup "$MOCK_VENV/bin/python" "$MOCK_SRC" "$MOCK_PORT" >"$MOCK_LOG" 2>&1 &
echo $! > "$MOCK_PID_FILE"

# Wait for the mock to accept traffic (up to 15s).
for _ in $(seq 1 30); do
  # Basic-auth probe: 200 with valid creds, 401 without — either means the server is up.
  status=$(curl -s -o /dev/null -w "%{http_code}" -u svc_dagster:DagsterDemo1 \
    "http://localhost:$MOCK_PORT/scheduler/api/processes?limit=1" 2>/dev/null || echo "000")
  [ "$status" = "200" ] && break
  sleep 0.5
done
ok "mock RunMyJobs live on http://localhost:$MOCK_PORT   (log: $MOCK_LOG)"

# ── 3. Install runmyjobs_integration component ───────────────────────────

info "3/5  Installing runmyjobs_integration component"
CLI="uvx --from dagster-community-components-cli dagster-component"
$CLI --refresh search runmyjobs_integration >/dev/null 2>&1 || true
$CLI add runmyjobs_integration --auto-install 2>&1 | tail -1

echo 'from .component import RunMyJobsIntegrationComponent, RunMyJobsJobSpec, RunMyJobsSourceTableSpec
__all__ = ["RunMyJobsIntegrationComponent", "RunMyJobsJobSpec", "RunMyJobsSourceTableSpec"]' \
  > "src/$PKG/components/runmyjobs_integration/__init__.py"

# ── 4. Write defs.yaml + env ─────────────────────────────────────────────

info "4/5  Writing defs.yaml (demo_mode: false, pointed at mock)"
rm -rf "src/$PKG/defs/runmyjobs_integration"
mkdir -p "src/$PKG/defs/runmyjobs_integration"
cat > "src/$PKG/defs/runmyjobs_integration/defs.yaml" <<EOF
type: $PKG.components.runmyjobs_integration.component.RunMyJobsIntegrationComponent

attributes:
  # demo_mode: false = hit the mock RMJ over real HTTP with Basic Auth.
  # Flip to true (or delete) to skip HTTP and simulate everything on stdout.
  demo_mode: false
  endpoint: "http://localhost:$MOCK_PORT"

  group_name: runmyjobs_integration
  runmyjobs_user_env: RUNMYJOBS_USER
  runmyjobs_password_env: RUNMYJOBS_PASSWORD

  max_retries: 2
  retry_delay_seconds: 60
  poll_interval_seconds: 1
  partition_start_date: "2024-01-01"

  jobs:
    - job_definition: EOD_BATCH_SETTLEMENT
      asset_name: rmj_eod_settlement
      description: >-
        End-of-day batch settlement. RunMyJobs orchestrates the underlying
        SAP / mainframe processes. Dagster submits via REST with the
        scheduled date from the partition key.
      application: DAILY_BATCH
      partition_type: CORE_BANKING
      sub_partition_type: SETTLEMENT
      queue: prod_queue

    - job_definition: REGULATORY_EXTRACT
      asset_name: rmj_regulatory_extract
      description: >-
        Regulatory reporting extract. Must complete before compliance
        dashboards refresh.
      application: DAILY_BATCH
      partition_type: COMPLIANCE
      sub_partition_type: REGULATORY_REPORTING
      queue: compliance_queue

  source_tables:
    - table_name: "CORE_BANKING.SETTLEMENT_LEDGER"
      asset_name: rmj_settlement_table
      produced_by: rmj_eod_settlement
EOF

cat > .env.demo <<EOF
export RUNMYJOBS_ENDPOINT="http://localhost:$MOCK_PORT"
export RUNMYJOBS_USER=svc_dagster
export RUNMYJOBS_PASSWORD=DagsterDemo1
EOF
ok "defs.yaml + .env.demo written"

# shellcheck disable=SC1091
source .env.demo

# ── 5. Verify + materialize one partition ────────────────────────────────

info "5/5  Verifying + materializing partition 2024-06-01"
uv run dg check defs 2>&1 | tail -3

PARTITION="${PARTITION:-2024-06-01}"
uv run dg launch --assets 'rmj_eod_settlement' --partition "$PARTITION" 2>&1 | \
  grep -E "SUBMIT|POLL|STDOUT|DONE|EVENT|RUN_SUCCESS|ASSET_MATERIALIZATION|ERROR" | \
  head -30

MOCK_PID="$(cat "$MOCK_PID_FILE")"
cat <<MSG

═══════════════════════════════════════════════════════════════════════
 RunMyJobs Integration demo — READY
═══════════════════════════════════════════════════════════════════════

Mock RMJ:       http://localhost:$MOCK_PORT   (PID: $MOCK_PID)
Mock creds:     svc_dagster / DagsterDemo1 (HTTP Basic)
Mock log tail:  $MOCK_LOG

Dagster surface:
  cd $PROJECT_DIR && source .env.demo
  uv run dg dev                  # UI at http://localhost:3000

You'll see:
  - Assets: rmj_eod_settlement / rmj_regulatory_extract (daily partitioned)
            rmj_settlement_table (source; lineage from rmj_eod_settlement)
  - Jobs:   runmyjobs_restart_process / runmyjobs_hold_application /
            runmyjobs_release_processes / runmyjobs_kill_process /
            runmyjobs_reconciliation
  - Sensors (STOPPED): runmyjobs_external_execution_monitor / runmyjobs_inbound_trigger
  - Schedule (STOPPED): runmyjobs_reconciliation_schedule (hourly)

Materialize another partition (exercises real REST calls):
  cd $PROJECT_DIR && source .env.demo
  uv run dg launch --assets 'rmj_eod_settlement' --partition 2024-06-02

Point at a real RunMyJobs instance:
  Set demo_mode: false in defs.yaml (already false) and change endpoint:
  export RUNMYJOBS_USER=<your-user>
  export RUNMYJOBS_PASSWORD=<your-password>
  # Edit defs.yaml endpoint: to your RMJ REST base URL
  uv run dg dev

Note on REST paths:
  RunMyJobs REST paths shift across versions (v6 / v9 / SAP-branded).
  The mock uses the modern JSON REST surface the component targets.
  If your instance uses a different prefix, bake it into 'endpoint'
  or fork _execute_runmyjobs in component.py to match.

Cleanup: kill $MOCK_PID && rm -rf $PROJECT_DIR
MSG
