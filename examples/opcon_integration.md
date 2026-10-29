# OpCon Integration — SMA Technologies / Continuous OpCon daily jobs as daily-partitioned Dagster assets

**Hybrid deployment for mainframe / AS400 / Windows-centric shops.** OpCon (SMA Technologies, now marketed under the Continuous brand) is workload-automation software that commonly orchestrates mainframe, AS400, and Windows batch together. This component keeps OpCon owning the legacy cross-platform batch it was designed for while Dagster orchestrates the cloud / analytics / AI pipeline that reads the outputs. One Dagster UI, correct lineage across both.

`demo_mode: true` (default) simulates the OpCon REST API lifecycle (token exchange + scheduleactions + dailyjobs status polling) on stdout — the whole component runs end-to-end with zero external dependencies. OpCon is on-prem software with no public Docker image or license-free trial, so the simulator is the only zero-license way to smoke-test the full surface.

## Components used

| Component | What it does |
|---|---|
| `opcon_integration` | Each declared OpCon Daily Job becomes a daily-partitioned Dagster asset with a retry policy. Ships 5 op-backed jobs (force-restart / hold / release / cancel / reconcile), 2 sensors (external-execution monitor + inbound trigger from OpCon via Dagster GraphQL), and an hourly reconciliation schedule. |

## What this demonstrates

- **Dagster wraps OpCon Daily Jobs.** Materializing an asset exchanges credentials for a token (`POST /tokens`), force-starts the job via `POST /api/scheduleactions` with the partition key as the job date, polls to terminal state (Waiting → Executing → Successful).
- **OpCon can trigger Dagster.** The `opcon_inbound_trigger` sensor confirms the wire-up when an OpCon job hits Dagster's GraphQL `launchRun` mutation.
- **Operational ops are Dagster jobs.** Force-restart / hold / release / cancel / reconcile are runnable from the Dagster UI.
- **Reconciliation as a scheduled job.** Every hour, query OpCon's confirmed `/api/dailyjobs/count_by_status` endpoint and report drift against Dagster's materialization ledger.

## Asset + job + sensor + schedule graph

```
Assets   ── opcon_eod_ledger_close         [daily partitioned, kinds: python + opcon]
         ── opcon_regulatory_extract       [daily partitioned, kinds: python + opcon]
         ── opcon_gl_ledger_table          [source, kinds: opcon + database]

Jobs     ── opcon_force_restart_job        (run config: {job_id})
         ── opcon_hold_job                 (run config: {job_id})
         ── opcon_release_job              (run config: {job_id})
         ── opcon_cancel_job               (run config: {job_id})
         ── opcon_reconciliation           (compare OpCon state vs Dagster; report drift)

Sensors  ── opcon_external_execution_monitor  (poll every 60s)
         ── opcon_inbound_trigger             (OpCon -> Dagster GraphQL)

Schedule ── opcon_reconciliation_schedule     (cron: 0 * * * *)
```

## Live output — one partition materialization in demo mode

Actual run logs (setup script materializes partition `2024-06-01` end-to-end):

```
opcon_eod_ledger_close - [AUTH]   POST https://opcon.internal/tokens -> token=sim-***** (Authorization: Token <token>)
opcon_eod_ledger_close - [ACTION] POST https://opcon.internal/api/scheduleactions {action: 'JOB:FORCERESTART', jobName: 'EOD_LEDGER_CLOSE', scheduleName: 'DAILY_FINANCE', jobDate: '2024-06-01'} -> jId=34199
opcon_eod_ledger_close - [POLL]   GET https://opcon.internal/api/dailyjobs/34199/status -> status=Waiting
opcon_eod_ledger_close - [POLL]   GET https://opcon.internal/api/dailyjobs/34199/status -> status=Executing
opcon_eod_ledger_close - [POLL]   GET https://opcon.internal/api/dailyjobs/34199/status -> status=Executing
opcon_eod_ledger_close - [POLL]   GET https://opcon.internal/api/dailyjobs/34199/status -> status=Successful
opcon_eod_ledger_close - [EVENTS] GET https://opcon.internal/api/dailyjobs/34199/events -> 6 events
opcon_eod_ledger_close - [DONE]   EOD_LEDGER_CLOSE -> Successful (jId: 34199, schedule: DAILY_FINANCE)
```

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_opcon_integration_demo.sh \
  -o setup_opcon_integration_demo.sh
bash setup_opcon_integration_demo.sh
```

Requirements: `uv`. $0, ~15s first run, zero external dependencies in demo mode.

```bash
cd opcon-demo
uv run dg dev
```

Then in the UI: click a partition (`2024-06-01` or later) → **Materialize**. Watch the full REST API trace stream into the run logs — every POLL shows the exact REST call the production executor makes.

The setup script also materializes partition `2024-06-01` at the end so you see a green run before you even open the UI.

Cleanup when done: `rm -rf opcon-demo`

## REST API endpoints exercised

```
POST /tokens                              — exchange credentials for a short-lived token
POST /api/scheduleactions                 — {action: "JOB:START"|"JOB:FORCERESTART"|..., jobName/jId, scheduleName, jobDate} -> jId
GET  /api/dailyjobs/{jId}/status          — poll job status
GET  /api/dailyjobs/{jId}/events          — job event history
GET  /api/dailyjobs/count_by_status       — status counts, used for reconciliation
```

Terminal statuses: `Successful | Failed | Cancelled | Skipped`. OpCon has the richest publicly documented API in this integration family — a full hosted OpenAPI/Swagger reference (`help.smatechnologies.com/opcon/core/api/22-0`), a live Swagger UI on every instance (`/api-doc/index.html`), and an official Java client on Maven Central. `JOB:START`/`JOB:FORCERESTART` are confirmed action names; `JOB:HOLD`/`JOB:RELEASE`/`JOB:CANCEL` follow the same documented convention but weren't individually confirmed from public sources — verify against your instance's Swagger UI before going to production.

## The hybrid deployment story

OpCon is designed for the shape where you're **not** migrating off it — OpCon keeps owning the mainframe/AS400/Windows-centric batch that only it orchestrates across those platforms together, Dagster owns the cloud / analytics / AI pipeline, both share a single Dagster UI with correct lineage. Common in finance/insurance/healthcare enterprises with decades of mainframe batch and no appetite to rip it out.

If you *are* doing a full migration off OpCon, the `airflow_dag_proxy` / `sql_transform` / `snowflake_workspace` / warehouse-migration components are the toolkit — this component keeps OpCon in the loop.

## See also

- Sister components (similar asset / op / sensor / schedule shape):
  - [`jams_integration`](jams_integration.md) — Fortra JAMS Scheduler
  - [`controlm_integration`](controlm_integration.md) — BMC Control-M
  - [`goanywhere_integration`](goanywhere_integration.md) — Fortra GoAnywhere MFT
- Component reference: <https://dagster-component-ui.vercel.app/c/opcon_integration>
- OpCon REST API reference: <https://help.smatechnologies.com/opcon/core/api/22-0>
- Walkthrough index: [examples/README.md](README.md)
