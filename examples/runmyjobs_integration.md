# RunMyJobs Integration — Redwood RunMyJobs jobs as daily-partitioned Dagster assets

**Validated end-to-end** (`demo_mode: true` simulates the full REST API lifecycle on stdout — zero external dependencies). Materialize any partition and you see the whole RMJ call trace (AUTH → SUBMIT → POLL × 5 → STDOUT → DONE → EVENT) in the run logs.

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

## Live output — one partition materialization in demo mode

```
[AUTH]   Authorization: Basic c3ZjX2RhZ3N0... (HTTP Basic)
[SUBMIT] POST .../scheduler/api/submitjob
  Payload: {"jobDefinition": "EOD_BATCH_SETTLEMENT", "application": "DAILY_BATCH",
            "queue": "prod_queue", "scheduledTime": "2024-06-01", "parameters": {}}
  Response: 201 Created — processId: RMJ-2418EC98
[POLL]   GET .../scheduler/api/processes/RMJ-2418EC98 -> status=Waiting Time
[POLL]   GET .../scheduler/api/processes/RMJ-2418EC98 -> status=Ready
[POLL]   GET .../scheduler/api/processes/RMJ-2418EC98 -> status=Running
[POLL]   GET .../scheduler/api/processes/RMJ-2418EC98 -> status=Running
[POLL]   GET .../scheduler/api/processes/RMJ-2418EC98 -> status=Completed
[STDOUT] GET .../scheduler/api/processes/RMJ-2418EC98/stdout -> 623 lines
[DONE]   EOD_BATCH_SETTLEMENT -> Completed (processId: RMJ-2418EC98, scheduledTime: 2024-06-01)
[EVENT]  POST .../scheduler/api/processes/RMJ-2418EC98/events
```

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
cd runmyjobs-demo
uv run dg dev
```

Then in the UI: click a partition (2024-06-01 or later) → **Materialize**. Watch the full REST API trace stream into the run logs.

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
