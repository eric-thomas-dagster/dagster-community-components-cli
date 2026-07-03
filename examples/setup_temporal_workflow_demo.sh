#!/usr/bin/env bash
# setup_temporal_workflow_demo.sh
#
# Full end-to-end Temporal + Dagster demo. Scaffolds a Dagster project that
# TRIGGERS a real Temporal workflow via `temporal_workflow_trigger`, plus
# declares an external asset via `external_temporal_workflow`, plus wires a
# `temporal_workflow_sensor` for observation-mode updates.
#
# Behind the scenes:
#   1. Starts a local `temporal server start-dev` (localhost:7233 + UI at :8233).
#   2. Starts a Python worker that registers a `PlanetSummaryWorkflow` — one
#      that fetches a Star Wars planet from the public SWAPI (https://swapi.dev)
#      via a Temporal activity.
#   3. Scaffolds a Dagster project with all three Temporal components.
#   4. Materializes the Dagster asset — which starts the Temporal workflow,
#      waits for the result, and captures it in asset metadata.
#   5. Runs the sensor once against the completed workflow to prove observation
#      also works.
#
# Everything is real — real Temporal gRPC calls, real Temporal server, real
# public API activity, real Dagster materialization.
#
# What it demonstrates
#   • TemporalWorkflowTriggerComponent — Dagster starts + waits for a workflow.
#   • ExternalTemporalWorkflowAsset — puts Temporal work in the catalog.
#   • TemporalWorkflowSensorComponent — observes terminal state.
#
# Cost: $0. Local Temporal dev server + free SWAPI public API.
#
# Requirements
#   • uv (https://docs.astral.sh/uv/)
#   • `temporal` CLI (brew install temporal)
#   • Internet access to swapi.dev
#
# Usage
#   ./setup_temporal_workflow_demo.sh                     # → temporal_demo
#   ./setup_temporal_workflow_demo.sh my_temporal_proj    # custom name

set -eo pipefail

PROJECT_NAME="${1:-temporal_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"
WORKER_LOG="${BASE_DIR}/${PROJECT_NAME}_worker.log"
SERVER_LOG="${BASE_DIR}/${PROJECT_NAME}_temporal_server.log"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_YELLOW='\033[0;33m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
warn()  { echo -e "${C_YELLOW}⚠${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; cleanup_procs; exit 1; }

SERVER_PID=""
WORKER_PID=""
cleanup_procs() {
  if [ -n "$WORKER_PID" ] && kill -0 "$WORKER_PID" 2>/dev/null; then
    info "Stopping worker (pid $WORKER_PID)…"
    kill "$WORKER_PID" 2>/dev/null || true
  fi
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    info "Stopping temporal server (pid $SERVER_PID)…"
    kill "$SERVER_PID" 2>/dev/null || true
  fi
}
trap cleanup_procs INT TERM

# ── Preflight ────────────────────────────────────────────────────────────────
command -v uvx >/dev/null 2>&1 || fail "uvx not found. Install uv: https://docs.astral.sh/uv/"
command -v temporal >/dev/null 2>&1 || fail "temporal CLI not found. Install: brew install temporal"

[ -d "$PROJECT_DIR" ] && fail "Directory already exists: $PROJECT_DIR"

info "Target project: $PROJECT_DIR"
info "temporal CLI: $(temporal --version | head -1)"

# Fail fast if 7233 is already occupied.
if lsof -iTCP:7233 -sTCP:LISTEN >/dev/null 2>&1; then
  fail "Port 7233 is already in use. Stop any running Temporal server first."
fi

# ── Start Temporal dev server ────────────────────────────────────────────────
info "Starting Temporal dev server on :7233 (UI on :8233)…"
temporal server start-dev --headless --db-filename "/tmp/${PROJECT_NAME}_temporal.db" >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!
sleep 5
if ! temporal operator cluster describe --address localhost:7233 >/dev/null 2>&1; then
  fail "Temporal dev server didn't come up. Check $SERVER_LOG"
fi
ok "Temporal server up (pid $SERVER_PID)"

# ── Scaffold project ─────────────────────────────────────────────────────────
info "Scaffolding Dagster project…"
uvx create-dagster project "$PROJECT_DIR" --uv-sync 2>&1 | tail -3 || fail "create-dagster failed"
cd "$PROJECT_DIR"

info "Installing dependencies (dagster-community-components + temporalio + httpx)…"
if [ -n "${DCC_SRC:-}" ] && [ -d "$DCC_SRC" ]; then
  info "Using local DCC source: $DCC_SRC"
  uv add --quiet "dagster-community-components @ ${DCC_SRC}" 'temporalio>=1.7.0' 'httpx>=0.27' || fail "uv add failed"
else
  uv add --quiet 'dagster-community-components @ git+https://github.com/eric-thomas-dagster/dagster-component-templates.git' 'temporalio>=1.7.0' 'httpx>=0.27' || fail "uv add failed"
fi
ok "Dependencies installed"

# ── Write the Temporal worker (registers the demo workflow) ─────────────────
mkdir -p "$PROJECT_DIR/temporal_worker"
cat > "$PROJECT_DIR/temporal_worker/worker.py" <<'WORKER'
"""Temporal worker registering PlanetSummaryWorkflow.

Fetches a Star Wars planet from https://swapi.dev via a Temporal activity.
Ctrl-C to stop.
"""
import asyncio
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    import httpx


@activity.defn
async def fetch_planet(planet_id: int) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(f"https://swapi.dev/api/planets/{planet_id}/")
        r.raise_for_status()
        return r.json()


@workflow.defn
class PlanetSummaryWorkflow:
    @workflow.run
    async def run(self, planet_id: int) -> dict:
        data = await workflow.execute_activity(
            fetch_planet,
            planet_id,
            start_to_close_timeout=timedelta(seconds=30),
        )
        return {
            "planet_id": planet_id,
            "name": data.get("name"),
            "climate": data.get("climate"),
            "terrain": data.get("terrain"),
            "population": data.get("population"),
            "diameter_km": data.get("diameter"),
        }


async def main() -> None:
    client = await Client.connect("localhost:7233", namespace="default")
    worker = Worker(
        client,
        task_queue="demo-tq",
        workflows=[PlanetSummaryWorkflow],
        activities=[fetch_planet],
    )
    print("[worker] polling task_queue='demo-tq' on localhost:7233")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
WORKER

ok "Wrote temporal_worker/worker.py"

# ── Start the worker ────────────────────────────────────────────────────────
info "Starting Temporal worker (task queue 'demo-tq')…"
uv run python temporal_worker/worker.py >"$WORKER_LOG" 2>&1 &
WORKER_PID=$!
sleep 3
if ! kill -0 "$WORKER_PID" 2>/dev/null; then
  fail "Worker died on startup. Check $WORKER_LOG"
fi
ok "Worker up (pid $WORKER_PID)"

# ── Write the Dagster defs (all three Temporal components) ──────────────────
# Each component lives in its own subdir with a `defs.yaml` — the layout
# that `load_from_defs_folder` recognizes.
TRIGGER_DIR="src/${PROJECT_NAME}/defs/planet_trigger"
EXTERNAL_DIR="src/${PROJECT_NAME}/defs/planet_external"
SENSOR_DIR="src/${PROJECT_NAME}/defs/planet_sensor"
mkdir -p "$TRIGGER_DIR" "$EXTERNAL_DIR" "$SENSOR_DIR"

# Trigger asset — the star of the show. Kicks the workflow and waits.
cat > "$TRIGGER_DIR/defs.yaml" <<'YAML'
type: dagster_community_components.TemporalWorkflowTriggerComponent
attributes:
  asset_key: temporal/demo/planet_summary
  workflow_type: PlanetSummaryWorkflow
  task_queue: demo-tq
  workflow_id: "planet-summary-{run_id}"
  workflow_arg: 1        # Tatooine
  target_host: localhost:7233
  namespace: default
  wait_for_result: true
  result_wait_timeout_seconds: 60
  group_name: temporal
  description: "Fetch a Star Wars planet through a Temporal workflow (SWAPI activity)."
YAML

# External asset — same key semantics but declare-only. Uncomment in a real
# deployment where Temporal owns the schedule; here it'd collide with the
# trigger, so we drop it in a sibling key for demonstration.
cat > "$EXTERNAL_DIR/defs.yaml" <<'YAML'
type: dagster_community_components.ExternalTemporalWorkflowAsset
attributes:
  asset_key: temporal/demo/planet_summary_external
  workflow_type: PlanetSummaryWorkflow
  namespace: default
  task_queue: demo-tq
  temporal_ui_url: http://localhost:8233
  group_name: temporal
  description: "Same workflow, declared external (illustrates the observation-only pattern)."
YAML

# Sensor — observes terminal Completed workflows and emits an AssetObservation.
cat > "$SENSOR_DIR/defs.yaml" <<'YAML'
type: dagster_community_components.TemporalWorkflowSensorComponent
attributes:
  sensor_name: temporal_planet_summary_done
  list_filter: "WorkflowType='PlanetSummaryWorkflow' AND ExecutionStatus='Completed'"
  job_name: __ASSET_JOB
  asset_key: temporal/demo/planet_summary_external
  asset_event_type: observation
  target_host: localhost:7233
  namespace: default
  minimum_interval_seconds: 30
YAML

ok "Wrote $DEFS_DIR/{planet_trigger,planet_external,planet_sensor}.yaml"

# ── Materialize the trigger asset ────────────────────────────────────────────
info "Materializing temporal/demo/planet_summary (starts + waits for workflow)…"
DAGSTER_MODULE="${PROJECT_NAME}.definitions"
uv run dagster asset materialize --select temporal/demo/planet_summary -m "$DAGSTER_MODULE" 2>&1 | tail -25 || fail "materialize failed"
ok "Workflow completed via Dagster asset"

# ── Report and cleanup ──────────────────────────────────────────────────────
echo
ok "Demo complete."
echo
cat <<EOF
Live services (still running):
  • Temporal server: http://localhost:8233  (Web UI)
  • Temporal worker: pid $WORKER_PID (logs: $WORKER_LOG)
  • Temporal server: pid $SERVER_PID (logs: $SERVER_LOG)

Open the Temporal UI to see the run:
  http://localhost:8233/namespaces/default/workflows

Or list from the CLI:
  temporal workflow list

Next steps:
  cd $PROJECT_NAME
  uv run dg dev          # open Dagster UI (browse the temporal group)
                         # the sensor 'temporal_planet_summary_done' will
                         # observe the completed workflow within 30s

To shut everything down:
  kill $WORKER_PID $SERVER_PID
  rm /tmp/${PROJECT_NAME}_temporal.db
EOF

# Detach from cleanup trap so services stay up after the script exits.
trap - INT TERM
