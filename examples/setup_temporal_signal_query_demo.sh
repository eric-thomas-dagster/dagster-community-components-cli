#!/usr/bin/env bash
# setup_temporal_signal_query_demo.sh
#
# The write-and-read half of the Dagster ↔ Temporal integration:
#
#   • TemporalSignalAssetComponent — Dagster asset SENDS state INTO a
#     long-lived Temporal workflow (signals).
#   • TemporalQueryAssetComponent — Dagster asset READS live state FROM
#     a long-lived Temporal workflow (queries).
#
# Together with the trio in setup_temporal_workflow_demo.sh (trigger +
# external + sensor), you now have all four modes of Dagster ↔ Temporal
# interaction: start, observe, push, pull.
#
# What the demo runs
#   1. Local temporal server start-dev.
#   2. A Python worker registering OrderBatchWorkflow (a long-lived
#      workflow with @signal handlers add_order + flush_batch and
#      @query handlers get_pending + stats).
#   3. Starts the workflow so it's running and awaiting signals.
#   4. Scaffolds a Dagster project with:
#        - temporal/batch/pending    (query_asset — reads current pending)
#        - temporal/batch/stats      (query_asset — reads counters)
#        - temporal/batch/add        (signal_asset — sends add_order)
#        - temporal/batch/flush      (signal_asset — sends flush_batch)
#   5. Materializes each asset in a scripted sequence:
#        query → materialize add → query → materialize flush → query
#      Each materialization is a real gRPC call against the workflow.
#
# Cost: $0. Local Temporal dev server, no external APIs.
#
# Requirements
#   • uv (https://docs.astral.sh/uv/)
#   • temporal CLI (brew install temporal)
#
# Usage
#   ./setup_temporal_signal_query_demo.sh                       # temporal_sq_demo/
#   ./setup_temporal_signal_query_demo.sh my_project            # custom name

set -eo pipefail

PROJECT_NAME="${1:-temporal_sq_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"
WORKER_LOG="${BASE_DIR}/${PROJECT_NAME}_worker.log"
SERVER_LOG="${BASE_DIR}/${PROJECT_NAME}_temporal_server.log"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_YELLOW='\033[0;33m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; cleanup_procs; exit 1; }

SERVER_PID=""; WORKER_PID=""
cleanup_procs() {
  [ -n "$WORKER_PID" ] && kill "$WORKER_PID" 2>/dev/null || true
  [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null || true
}
trap cleanup_procs INT TERM

command -v uvx >/dev/null 2>&1 || fail "uvx not found. Install uv: https://docs.astral.sh/uv/"
command -v temporal >/dev/null 2>&1 || fail "temporal CLI not found. Install: brew install temporal"
[ -d "$PROJECT_DIR" ] && fail "Directory already exists: $PROJECT_DIR"

if lsof -iTCP:7233 -sTCP:LISTEN >/dev/null 2>&1; then
  fail "Port 7233 already in use. Stop any running Temporal server first."
fi

info "Starting Temporal dev server on :7233 (UI on :8233)…"
temporal server start-dev --headless --db-filename "/tmp/${PROJECT_NAME}_temporal.db" >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!
sleep 5
temporal operator cluster describe --address localhost:7233 >/dev/null 2>&1 || fail "Temporal server failed to start."
ok "Temporal server up (pid $SERVER_PID)"

info "Scaffolding Dagster project…"
uvx create-dagster project "$PROJECT_DIR" --uv-sync 2>&1 | tail -3 || fail "create-dagster failed"
cd "$PROJECT_DIR"

info "Installing deps…"
if [ -n "${DCC_SRC:-}" ] && [ -d "$DCC_SRC" ]; then
  info "Using local DCC source: $DCC_SRC"
  uv add --quiet "dagster-community-components @ ${DCC_SRC}" 'temporalio>=1.7.0' || fail "uv add failed"
else
  uv add --quiet 'dagster-community-components>=0.10.0' 'temporalio>=1.7.0' || fail "uv add failed"
fi
ok "Dependencies installed"

# ── Worker with signal + query handlers ─────────────────────────────────────
mkdir -p "$PROJECT_DIR/temporal_worker"
cat > "$PROJECT_DIR/temporal_worker/worker.py" <<'WORKER'
"""Long-lived Temporal workflow with @signal and @query handlers."""
import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.client import Client
from temporalio.worker import Worker


@workflow.defn
class OrderBatchWorkflow:
    def __init__(self) -> None:
        self._pending: list[dict] = []
        self._flushed_batches: list[list[dict]] = []
        self._flush_now: bool = False
        self._shutdown: bool = False

    @workflow.signal
    def add_order(self, order: dict) -> None:
        self._pending.append(order)

    @workflow.signal
    def flush_batch(self, args: dict = {}) -> None:
        workflow.logger.info(f"flush_batch: {args}")
        self._flush_now = True

    @workflow.signal
    def shutdown(self) -> None:
        self._shutdown = True

    @workflow.query
    def get_pending(self) -> list[dict]:
        return list(self._pending)

    @workflow.query
    def stats(self) -> dict:
        return {
            "pending_count": len(self._pending),
            "flushed_batch_count": len(self._flushed_batches),
            "total_flushed_orders": sum(len(b) for b in self._flushed_batches),
        }

    @workflow.run
    async def run(self) -> dict:
        self._pending.append({"id": "seed-1", "amount": 10.0})
        self._pending.append({"id": "seed-2", "amount": 20.0})
        while not self._shutdown:
            await workflow.wait_condition(
                lambda: self._flush_now or self._shutdown,
                timeout=timedelta(hours=1),
            )
            if self._flush_now:
                if self._pending:
                    self._flushed_batches.append(list(self._pending))
                    self._pending.clear()
                self._flush_now = False
        return {"flushed_batch_count": len(self._flushed_batches)}


async def main() -> None:
    client = await Client.connect("localhost:7233", namespace="default")
    worker = Worker(client, task_queue="sq-demo-tq", workflows=[OrderBatchWorkflow])
    print("[worker] polling task_queue='sq-demo-tq'")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
WORKER
ok "Wrote worker.py"

info "Starting worker…"
uv run python temporal_worker/worker.py >"$WORKER_LOG" 2>&1 &
WORKER_PID=$!
sleep 3
kill -0 "$WORKER_PID" 2>/dev/null || fail "Worker died. Check $WORKER_LOG"
ok "Worker up (pid $WORKER_PID)"

info "Starting the long-lived OrderBatchWorkflow…"
temporal workflow start \
  --address localhost:7233 --namespace default \
  --workflow-id order-batch-demo \
  --task-queue sq-demo-tq \
  --type OrderBatchWorkflow 2>&1 | tail -3
sleep 2
ok "Workflow order-batch-demo running (awaiting signals)"

# ── Dagster defs (2 signals + 2 queries) ────────────────────────────────────
mkdir -p "src/${PROJECT_NAME}/defs/pending"     "src/${PROJECT_NAME}/defs/stats"    \
         "src/${PROJECT_NAME}/defs/add"          "src/${PROJECT_NAME}/defs/flush"

cat > "src/${PROJECT_NAME}/defs/pending/defs.yaml" <<'YAML'
type: dagster_community_components.TemporalQueryAssetComponent
attributes:
  asset_key: temporal/batch/pending
  workflow_id: order-batch-demo
  query_name: get_pending
  target_host: localhost:7233
  group_name: temporal
YAML

cat > "src/${PROJECT_NAME}/defs/stats/defs.yaml" <<'YAML'
type: dagster_community_components.TemporalQueryAssetComponent
attributes:
  asset_key: temporal/batch/stats
  workflow_id: order-batch-demo
  query_name: stats
  target_host: localhost:7233
  group_name: temporal
YAML

cat > "src/${PROJECT_NAME}/defs/add/defs.yaml" <<'YAML'
type: dagster_community_components.TemporalSignalAssetComponent
attributes:
  asset_key: temporal/batch/add
  workflow_id: order-batch-demo
  signal_name: add_order
  signal_arg:
    id: dagster-run-{run_id}
    amount: 99.99
  target_host: localhost:7233
  group_name: temporal
YAML

cat > "src/${PROJECT_NAME}/defs/flush/defs.yaml" <<'YAML'
type: dagster_community_components.TemporalSignalAssetComponent
attributes:
  asset_key: temporal/batch/flush
  workflow_id: order-batch-demo
  signal_name: flush_batch
  signal_arg:
    reason: dagster-scheduled-flush
    requested_at: "{run_id}"
  target_host: localhost:7233
  group_name: temporal
YAML
ok "Wrote 4 defs (2 queries, 2 signals)"

# ── Scripted sequence ────────────────────────────────────────────────────────
DM="${PROJECT_NAME}.definitions"
info "Query 1 — initial stats:"
uv run dagster asset materialize --select temporal/batch/stats -m "$DM" 2>&1 | tail -3

info "Signal 1 — add a Dagster-sourced order:"
uv run dagster asset materialize --select temporal/batch/add -m "$DM" 2>&1 | tail -3

info "Query 2 — pending list after add:"
uv run dagster asset materialize --select temporal/batch/pending -m "$DM" 2>&1 | tail -3

info "Signal 2 — flush the batch:"
uv run dagster asset materialize --select temporal/batch/flush -m "$DM" 2>&1 | tail -3

info "Query 3 — stats after flush:"
uv run dagster asset materialize --select temporal/batch/stats -m "$DM" 2>&1 | tail -3

echo
ok "Demo complete."
echo
cat <<EOF
Live services (still running):
  • Temporal UI: http://localhost:8233/namespaces/default/workflows
  • Worker: pid $WORKER_PID (logs: $WORKER_LOG)
  • Server: pid $SERVER_PID (logs: $SERVER_LOG)

Open Dagster UI:
  cd $PROJECT_NAME
  uv run dg dev

To stop everything:
  kill $WORKER_PID $SERVER_PID
  rm /tmp/${PROJECT_NAME}_temporal.db
EOF

trap - INT TERM
