#!/usr/bin/env bash
# prefect_workspace — auto-discover every Prefect deployment, zero per-flow YAML.
#
# The story:
#   - Two Prefect flows are already deployed and served against a local
#     Prefect server: daily_etl (has its own cron schedule) and
#     nightly_report (writes a row-count table artifact every run).
#   - ONE PrefectWorkspaceComponent block discovers both via the Prefect API
#     and builds a triggerable Dagster asset for each — no per-deployment
#     YAML, unlike PrefectFlowRunAssetComponent.
#   - Two things stay explicit, per deployment, never global:
#       * daily_etl's Prefect-side cron is paused by this script, then
#         opted into a mirrored Dagster ScheduleDefinition via
#         assets_by_name.daily_etl/main.schedule: true (auto_schedule is
#         just the master switch — it does nothing without this).
#       * nightly_report's row-count-check artifact is declared as an
#         AssetCheckSpec via assets_by_name.nightly_report/main.check_names
#         — not a global check name applied to every discovered deployment.
#
# You watch:
#   - Prefect UI (http://127.0.0.1:4200) — Deployments tab (cron paused on
#     daily_etl/main), Flow Runs, and nightly_report's row-count-check
#     artifact.
#   - Dagster UI (http://localhost:3000) — both auto-discovered assets, the
#     row_count_check on nightly_report_main, the mirrored schedule on
#     daily_etl_main, and streamed Prefect log lines in the run log.

set -eo pipefail

PROJECT_DIR="${1:-prefect-workspace-demo}"
COMMIT_SHA="${COMMIT_SHA:-main}"

if ! command -v uv >/dev/null 2>&1; then echo "✗ uv required"; exit 1; fi

if [ -n "$DCC_LOCAL_PATH" ]; then
  DCC_SRC="dagster-community-components @ file://$DCC_LOCAL_PATH"
else
  DCC_SRC="dagster-community-components @ https://github.com/eric-thomas-dagster/dagster-component-templates/archive/$COMMIT_SHA.zip"
fi

rm -rf "$PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync 2>&1 | tail -3
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"

# --- Deps -----------------------------------------------------------------
uv add -q "$DCC_SRC" prefect

export DAGSTER_HOME="$PROJECT_ABS/.dagster_home"
mkdir -p "$DAGSTER_HOME"

# --- Two Prefect flows, served together, no Dagster awareness --------------
mkdir -p prefect_worker
cat > prefect_worker/flows.py <<'PY'
"""Two ordinary Prefect flows, served together. Neither one knows Dagster
exists — PrefectWorkspaceComponent discovers both purely through the Prefect
API (read_deployments) and builds a Dagster asset for each on its own."""
from prefect import flow, get_run_logger, serve
from prefect.artifacts import create_table_artifact


@flow(name="daily_etl")
def daily_etl(rows: int = 100) -> dict:
    logger = get_run_logger()
    logger.info(f"daily_etl: processing {rows} rows")
    logger.info("daily_etl: done")
    return {"rows_processed": rows}


@flow(name="nightly_report")
def nightly_report(min_rows: int = 10) -> dict:
    """Writes a table artifact recording a row-count check. Dagster's
    check_names field (opted in for THIS deployment only, via
    assets_by_name) turns this into a real AssetCheckResult — the dash in
    the artifact key becomes an underscore to satisfy Dagster's check-name
    rules (row-count-check -> row_count_check)."""
    logger = get_run_logger()
    row_count = 42
    passed = row_count >= min_rows
    logger.info(f"nightly_report: row_count={row_count} min_rows={min_rows} passed={passed}")
    create_table_artifact(
        key="row-count-check",
        table=[{"passed": passed, "row_count": row_count, "min_rows": min_rows}],
        description="Row count threshold check for the nightly report.",
    )
    return {"row_count": row_count, "passed": passed}


if __name__ == "__main__":
    serve(
        daily_etl.to_deployment(name="main", cron="0 2 * * *"),
        nightly_report.to_deployment(name="main"),
    )
PY

# --- Start local Prefect server + serve both flows -------------------------
export PREFECT_API_URL=http://127.0.0.1:4200/api

echo ""
echo ">>> Starting local Prefect server (background — logs in prefect_worker/server.log)"
uv run prefect server start >prefect_worker/server.log 2>&1 &
PREFECT_SERVER_PID=$!
echo "    server pid: $PREFECT_SERVER_PID"

for i in $(seq 1 30); do
  sleep 2
  if uv run curl -sf -m 2 "$PREFECT_API_URL/hello" >/dev/null 2>&1; then
    echo "    server up (attempt $i)"; break
  fi
  if uv run python -c "import urllib.request; urllib.request.urlopen('$PREFECT_API_URL/hello', timeout=2)" 2>/dev/null; then
    echo "    server up (attempt $i)"; break
  fi
  if [ $i -eq 30 ]; then
    echo "    ✗ server didn't come up — check prefect_worker/server.log"; kill $PREFECT_SERVER_PID 2>/dev/null; exit 1
  fi
done

echo ""
echo ">>> Serving both flows in background (prefect_worker/worker.log)"
uv run python prefect_worker/flows.py >prefect_worker/worker.log 2>&1 &
WORKER_PID=$!
echo "    worker pid: $WORKER_PID"

for i in $(seq 1 20); do
  sleep 2
  if uv run python -c "
import asyncio
from prefect.client.orchestration import get_client
async def _c():
    async with get_client() as c:
        deps = await c.read_deployments()
        names = {d.name for d in deps}
        return {'main'} <= names and len(deps) >= 2
raise SystemExit(0 if asyncio.run(_c()) else 1)
" 2>/dev/null; then
    echo "    both deployments registered (attempt $i)"; break
  fi
  if [ $i -eq 20 ]; then
    echo "    ✗ deployments didn't appear — check prefect_worker/worker.log"; kill $PREFECT_SERVER_PID $WORKER_PID 2>/dev/null; exit 1
  fi
done

# --- Pause daily_etl/main's Prefect-side cron -------------------------------
# auto_schedule's per-deployment opt-in (assets_by_name.daily_etl/main.schedule:
# true, set below) raises at Dagster defs-load time unless this is paused
# first — the explicit precondition that stops Prefect and Dagster from ever
# firing the same deployment on the same tick.
echo ""
echo ">>> Pausing daily_etl/main's Prefect-side cron (precondition for auto_schedule)"
uv run python -c "
import asyncio
from prefect.client.orchestration import get_client

async def _pause():
    async with get_client() as client:
        dep = await client.read_deployment_by_name('daily_etl/main')
        for ds in dep.schedules or []:
            await client.update_deployment_schedule(dep.id, ds.id, active=False)
        print(f'    paused {len(dep.schedules or [])} schedule(s) on daily_etl/main')

asyncio.run(_pause())
"

# --- Dagster: one PrefectWorkspaceComponent block, no per-deployment YAML --
PKG="$(ls src/ | head -1)"
DEFS="src/$PKG/defs"

mkdir -p "$DEFS/prefect_workspace"
cat > "$DEFS/prefect_workspace/defs.yaml" <<'YAML'
type: dagster_community_components.PrefectWorkspaceComponent
attributes:
  workspace:
    api_url: http://127.0.0.1:4200/api
  stream_logs: true
  stream_artifacts: true
  auto_schedule: true
  assets_by_name:
    daily_etl/main:
      schedule: true
    nightly_report/main:
      check_names: [row_count_check]
  group_name: prefect_workspace_demo
YAML

echo ""
echo ">>> dg check defs — both deployments discovered from ONE component block"
if ! uv run dg check defs 2>&1 | tail -8; then
  echo "    ✗ dg check failed"
  kill $PREFECT_SERVER_PID $WORKER_PID 2>/dev/null
  exit 1
fi

echo ""
echo ">>> dg list defs — confirm the auto-discovered assets + mirrored schedule"
uv run dg list defs 2>&1 | tail -20

echo ""
echo ">>> Materializing prefect/daily_etl_main"
uv run dg launch --assets prefect/daily_etl_main 2>&1 | tail -6

echo ""
echo ">>> Materializing prefect/nightly_report_main (streams logs + the row-count-check artifact + AssetCheckResult)"
uv run dg launch --assets prefect/nightly_report_main 2>&1 | tail -20

echo ""
echo ">>> Prefect flow run summary (from local server):"
uv run python - <<'PY'
import asyncio
from prefect.client.orchestration import get_client
async def _summary():
    async with get_client() as c:
        runs = await c.read_flow_runs()
        for fr in runs[:6]:
            state = getattr(fr.state, "name", "?") if fr.state else "?"
            print(f"    {str(fr.id)[:8]}…  {state:<10}  {fr.name}")
asyncio.run(_summary())
PY

cat <<DONE

✓ prefect_workspace demo done.

The story you can walk through:
  - Prefect server up on http://127.0.0.1:4200 (pid $PREFECT_SERVER_PID)
  - Prefect worker serving 'daily_etl/main' (cron paused) and
    'nightly_report/main' (pid $WORKER_PID)
  - ONE PrefectWorkspaceComponent block in $DEFS/prefect_workspace/defs.yaml
    discovered both deployments and built:
      prefect/daily_etl_main         — asset + mirrored ScheduleDefinition
      prefect/nightly_report_main    — asset + row_count_check AssetCheckSpec
  - Add a third Prefect deployment and re-run 'dg check defs' — zero new YAML.

Browse:
  Prefect UI:  http://127.0.0.1:4200  → Deployments / Flow Runs / Artifacts
  Dagster UI:  cd $PROJECT_ABS && uv run dg dev  → localhost:3000
               (Assets tab, click nightly_report_main → Checks tab)

To stop the Prefect server + worker when done:
  kill $PREFECT_SERVER_PID $WORKER_PID

To point at Prefect Cloud instead of local, or narrow discovery to specific
deployments, edit $DEFS/prefect_workspace/defs.yaml — see prefect_workspace.md.

Cleanup:
  kill $PREFECT_SERVER_PID $WORKER_PID; rm -rf $PROJECT_ABS
DONE
