# RunMyJobs Integration — Redwood RunMyJobs jobs as daily-partitioned Dagster assets

**Validated end-to-end** — the setup script spins up a **mock RunMyJobs REST server** (FastAPI, HTTP Basic Auth, realistic state machine `Waiting Time → Ready → Running → Completed`) inside the project directory, then points the component at it with `demo_mode: false`. Materialization exercises the REAL `_execute_runmyjobs` code path over real HTTP — not the in-process stdout simulator — so the whole Dagster surface is validated against a working REST endpoint.

Prefer the in-process simulator? Set `demo_mode: true` and skip the mock server entirely — same asset shape, same run-log trace, no external process.

## Components used

| Component | What it does |
|---|---|
| `runmyjobs_integration` | Each declared RunMyJobs JobDefinition becomes a daily-partitioned Dagster asset with a retry policy. Ships 5 op-backed jobs (restart / hold / release / kill / reconcile), 2 sensors (external-execution monitor + inbound trigger from RMJ via Dagster GraphQL), and an hourly reconciliation schedule. |

## What this demonstrates

- **Dagster wraps RunMyJobs JobDefinitions.** Materializing an asset submits the JobDefinition to RMJ with the partition key as `scheduledTime`, polls to terminal state (Waiting Time → Ready → Running → Completed), retrieves stdout, posts a feedback event.
- **RunMyJobs can trigger Dagster.** The `runmyjobs_inbound_trigger` sensor confirms the wire-up when an RMJ post-step hits Dagster's GraphQL `launchRun` mutation.
- **Operational ops are Dagster jobs.** Restart / hold / release / kill / reconcile are runnable from the Dagster UI.
- **Reconciliation as a scheduled job.** Every hour, compare RunMyJobs' state to Dagster's materialization ledger and report drift.

## Asset + job + sensor + schedule graph

```
Assets   ── rmj_eod_settlement            [daily partitioned, kinds: python + runmyjobs]
         ── rmj_regulatory_extract        [daily partitioned, kinds: python + runmyjobs]
         ── rmj_settlement_table          [source, kinds: runmyjobs + database]

Jobs     ── runmyjobs_restart_process     (run config: {process_id})
         ── runmyjobs_hold_application    (run config: {application, queue})
         ── runmyjobs_release_processes   (run config: {application})
         ── runmyjobs_kill_process        (run config: {process_id})
         ── runmyjobs_reconciliation      (compare RMJ state vs Dagster; report drift)

Sensors  ── runmyjobs_external_execution_monitor  (poll every 60s)
         ── runmyjobs_inbound_trigger             (RMJ -> Dagster GraphQL)

Schedule ── runmyjobs_reconciliation_schedule     (cron: 0 * * * *)
```

## Live output — one partition materialization against the mock

Actual run logs (setup script materializes partition `2024-06-01` end-to-end):

```
[SUBMIT] POST http://localhost:8890/scheduler/api/submitjob — EOD_BATCH_SETTLEMENT
[POLL]   EOD_BATCH_SETTLEMENT -> Waiting Time
[POLL]   EOD_BATCH_SETTLEMENT -> Ready
[POLL]   EOD_BATCH_SETTLEMENT -> Running
[POLL]   EOD_BATCH_SETTLEMENT -> Running
[POLL]   EOD_BATCH_SETTLEMENT -> Completed
[STDOUT] 7 lines
[EVENT]  Sent completion event
ASSET_MATERIALIZATION - Materialized value rmj_eod_settlement.
RUN_SUCCESS
```

Every poll is a real HTTP GET against the mock server. Every state transition is driven by the mock's state machine (deterministic 4-poll settle → `Completed`).

## Mock RunMyJobs server (what the setup script ships)

The setup script writes a ~150-line FastAPI app inside `<project>/rmj-mock/mock_rmj.py`, launches it in an isolated venv (`<project>/rmj-mock/venv/`) on `localhost:8890`, and points the component at it with HTTP Basic Auth (`svc_dagster` / `DagsterDemo1`).

Endpoints served (matching the modern RunMyJobs JSON REST surface):

```
POST /scheduler/api/submitjob                    — create process (returns processId)
GET  /scheduler/api/processes/{id}               — status; state machine advances each poll
GET  /scheduler/api/processes/{id}/stdout        — fake stdout output
POST /scheduler/api/processes/{id}/events        — accept completion event
POST /scheduler/api/processes/{id}/rerun         — restart process (restart op)
POST /scheduler/api/processes/{id}/kill          — kill process (kill op)
POST /scheduler/api/applications/{app}/hold      — hold all processes in app (hold op)
POST /scheduler/api/applications/{app}/release   — release held processes (release op)
GET  /scheduler/api/processes?since=...&limit=…  — list for reconciliation
```

Every endpoint requires HTTP Basic Auth. Held applications reject subsequent `/submitjob` requests. Kill flips process status to `Killed`. Rerun resets the poll counter so the state machine walks again.

All state lives in-memory in the mock — restart the mock to reset.

## Why a mock instead of a Docker image

Redwood's PoC Docker images are license-gated (require a temporary key from Redwood Support). The mock exposes exactly the 9 endpoints the component hits with realistic responses, ships in an isolated Python venv inside the project directory (no `/tmp` pollution, no Windows portability trap), and — critically — exercises the **real** `_execute_runmyjobs` code path so you get true validation of the Dagster surface. When you swap in a real RMJ instance (Redwood-provisioned or SAP Redwood Schedule), only the `endpoint` + `RUNMYJOBS_USER` / `RUNMYJOBS_PASSWORD` env vars change.

Asset metadata captured on every materialization: `external_process_id`, `status`, `scheduled_time`, `duration_seconds`, `demo_mode`.

## Terminology cheat-sheet — Control-M ↔ RunMyJobs

For teams running both or migrating between them:

| Control-M | RunMyJobs |
|---|---|
| Job | JobDefinition |
| Folder | Application |
| Agent / Host | Queue |
| ODATE | scheduledTime |
| runId | processId |
| "Ended OK" / "Ended Not OK" | "Completed" / "Error" |

The [`controlm_integration`](controlm_integration.md) sister component has the same asset / op / sensor / schedule surface, so shops running both can present a single Dagster pane of glass over both schedulers.

## Point at a real RunMyJobs instance

Set `demo_mode: false` in `defs.yaml` and override `endpoint`:

```yaml
attributes:
  demo_mode: false
  endpoint: "https://runmyjobs.prod.internal:8443/RunMyJobs/api-rest"
  ...
```

Then export credentials:

```bash
export RUNMYJOBS_USER=svc_dagster
export RUNMYJOBS_PASSWORD='<your-password>'
```

**API-path caveat.** RunMyJobs REST paths shift across versions (v6 vs v9 vs SAP-branded builds vary in prefix and payload shape). The URIs in this component target the modern JSON REST surface. If your instance uses a different prefix (`/scheduler/api/v1/…` vs `/api-rest/scheduler/…`), either bake it into `endpoint` or fork `_execute_runmyjobs` in `component.py` to match. Demo mode is unaffected.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_runmyjobs_integration_demo.sh | bash
cd runmyjobs-demo && source .env.demo
uv run dg dev
```

Then in the UI: click a partition (2024-06-01 or later) → **Materialize**. Watch the full REST API trace stream into the run logs — every POLL is a real HTTP call to `http://localhost:8890`.

The setup script also materializes partition `2024-06-01` at the end so you see a green run before you even open the UI.

Cleanup when done: `kill $(cat runmyjobs-demo/rmj-mock/mock.pid) && rm -rf runmyjobs-demo`

## REST API endpoints exercised

```
POST /scheduler/api/submitjob                    — submit a JobDefinition
GET  /scheduler/api/processes/{id}               — poll process status
GET  /scheduler/api/processes/{id}/stdout        — process output
POST /scheduler/api/processes/{id}/events        — post feedback event
POST /scheduler/api/processes/{id}/rerun         — restart a failed process
POST /scheduler/api/processes/{id}/kill          — kill a running process
POST /scheduler/api/applications/{app}/hold      — hold all processes in an application
POST /scheduler/api/applications/{app}/release   — release held processes
GET  /scheduler/api/processes?since=1h&limit=200 — list for reconciliation
```

## The hybrid deployment story

RunMyJobs is designed for the shape where you're **not** migrating off it — RMJ keeps owning the batch that only RMJ can own (SAP process chains, mainframe wrappers, decades of scheduling), Dagster owns the cloud / analytics / AI pipeline, both share a single Dagster UI with correct lineage.

If you *are* doing a full migration, the `airflow_dag_proxy` / `sql_transform` / `snowflake_workspace` / warehouse-migration components are the toolkit.

## See also

- Sister component: [`controlm_integration`](controlm_integration.md) — same shape for BMC Control-M
- Component reference: <https://dagster-component-ui.vercel.app/c/runmyjobs_integration>
- Walkthrough index: [examples/README.md](README.md)
