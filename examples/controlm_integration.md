# Control-M Integration — BMC Control-M jobs as daily-partitioned Dagster assets

**Validated end-to-end** (`demo_mode: true` simulates the full Automation API lifecycle on stdout — zero external dependencies). Materialize any partition and you see the whole Control-M call trace (LOGIN → SUBMIT → POLL × 5 → OUTPUT → DONE → FEEDBACK → LOGOUT) in the run logs.

## Components used

| Component | What it does |
|---|---|
| `controlm_integration` | Each declared Control-M job becomes a daily-partitioned Dagster asset with a retry policy. Ships 5 op-backed jobs (restart / hold / free / kill / reconcile), 2 sensors (external-execution monitor + inbound trigger from Control-M via Dagster GraphQL), and an hourly reconciliation schedule. |

## What this demonstrates

- **Dagster wraps Control-M jobs.** Materializing an asset submits the job to Control-M with the partition key as ODATE, polls to terminal state, retrieves the spool, sends a feedback event.
- **Control-M can trigger Dagster.** The `controlm_inbound_trigger` sensor confirms the wire-up when a Control-M post-processing step hits Dagster's GraphQL `launchRun` mutation.
- **Operational ops are Dagster jobs.** Restart / hold / free / kill / reconcile are runnable from the Dagster UI. Kill a runaway CTM-XXXX from the same pane as your dbt runs.
- **Reconciliation as a scheduled job.** Every hour, compare Control-M's state to Dagster's materialization ledger and report drift.

## Asset + job + sensor + schedule graph

```
Assets   ── controlm_eod_settlement        [daily partitioned, kinds: python + control-m]
         ── controlm_regulatory_extract    [daily partitioned, kinds: python + control-m]
         ── controlm_settlement_table      [source, kinds: control-m + database]

Jobs     ── controlm_restart_job           (run config: {run_id})
         ── controlm_hold_folder           (run config: {folder, server})
         ── controlm_free_folder           (run config: {folder})
         ── controlm_kill_job              (run config: {job_id})
         ── controlm_reconciliation        (compare CTM state vs Dagster; report drift)

Sensors  ── controlm_external_execution_monitor   (poll every 60s)
         ── controlm_inbound_trigger              (Control-M -> Dagster GraphQL)

Schedule ── controlm_reconciliation_schedule      (cron: 0 * * * *)
```

## Live output — one partition materialization in demo mode

```
[LOGIN]    POST .../session/login
  Response: 200 OK — token acquired
[SUBMIT]   POST .../run/order
  Payload: {"ctm": "ctm-agent-prod-01", "folder": "DAILY_BATCH",
            "jobs": "EOD_BATCH_SETTLEMENT", "hold": "false", "odate": "2024-06-01"}
  Response: 200 OK — runId: CTM-8E5D42A1
[POLL]     GET .../run/jobs/status?runId=CTM-8E5D42A1 -> Submitted
[POLL]     GET .../run/jobs/status?runId=CTM-8E5D42A1 -> Wait Condition
[POLL]     GET .../run/jobs/status?runId=CTM-8E5D42A1 -> Executing
[POLL]     GET .../run/jobs/status?runId=CTM-8E5D42A1 -> Executing
[POLL]     GET .../run/jobs/status?runId=CTM-8E5D42A1 -> Ended OK
[OUTPUT]   GET {outputURI} -> 847 lines
[DONE]     EOD_BATCH_SETTLEMENT -> Ended OK (runId: CTM-8E5D42A1, ODATE: 2024-06-01)
[FEEDBACK] POST .../run/event/CTM-8E5D42A1
[LOGOUT]   POST .../session/logout
```

Asset metadata captured on every materialization: `external_job_id` (CTM run id), `status`, `odate`, `duration_seconds`, `demo_mode`.

## The hybrid deployment story

Control-M is designed for the shape where you're **not** migrating off it — Control-M keeps owning the batch that only Control-M can own (JCL submission, JES scheduling, mainframe agents, decades of dependency graph), Dagster keeps owning the cloud / analytics / AI pipeline, and both are visible from the same Dagster UI with correct lineage.

If you *are* doing a full Control-M lift-off, the `airflow_dag_proxy` / `sql_transform` / `snowflake_workspace` / warehouse-migration components are the toolkit.

## Point at a real Control-M

Set `demo_mode: false` in `defs.yaml` and (optionally) override `endpoint`:

```yaml
attributes:
  demo_mode: false
  endpoint: "https://controlm.prod.internal:8443/automation-api"
  ...
```

Then export credentials:

```bash
export CONTROLM_USER=svc_dagster
export CONTROLM_PASSWORD='<your-password>'
```

Everything else stays exactly as the demo — same job list, same partition shape, same ops.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_controlm_integration_demo.sh | bash
cd controlm-demo
uv run dg dev
```

Then in the UI: click a partition (2024-06-01 or later) → **Materialize**. Watch the full Automation API trace stream into the run logs.

## Automation API endpoints exercised

```
POST /session/login              — Bearer JWT
POST /run/order                  — submit a job
GET  /run/jobs/status?runId=…    — poll status
GET  {outputURI}                 — spool output
POST /run/event/{runId}          — post feedback event
POST /run/runNow                 — restart a failed job
DELETE /run/job/{id}/kill        — kill a running job
POST /session/logout             — end session
```

## Companion — RunMyJobs

Shops running both Control-M and Redwood RunMyJobs can pair this with [`runmyjobs_integration`](runmyjobs_integration.md) — same asset / op / sensor / schedule surface, one Dagster pane of glass over both schedulers.

## See also

- Component reference: <https://dagster-component-ui.vercel.app/c/controlm_integration>
- Walkthrough index: [examples/README.md](README.md)
