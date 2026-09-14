# IWS Integration — IBM Workload Scheduler (TWS) jobs as daily-partitioned Dagster assets

**Validated end-to-end** — the setup script scaffolds a Dagster project, installs the `iws_integration` component in `demo_mode: true`, and materializes one partition. The `demo_mode` simulator runs the whole IWS REST lifecycle (AUTH → SUBMIT → POLL through `Waiting → Ready → Running → Succ` → STDLIST) on stdout with **zero external dependencies and no IBM entitlement**. Flip `demo_mode: false` + set `IWS_USER` / `IWS_PASSWORD` and the same asset code hits your real IWS distributed engine (`/twsd/v1`) or z/OS engine (`/twsz/v1`) unchanged.

## Components used

| Component | What it does |
|---|---|
| `iws_integration` | Each declared IWS Job becomes a daily-partitioned Dagster asset with a retry policy. Ships 5 op-backed jobs (rerun / hold / release / kill / reconcile), 2 sensors (external-execution monitor + inbound trigger from IWS via Dagster GraphQL), and an hourly reconciliation schedule. |

## The hybrid deployment story — banking, insurance, government

IBM Workload Scheduler (still widely called TWS after the Tivoli name) is entrenched exactly where wholesale migrations are hardest: **retail + investment banking, insurance carriers, and federal/state government**. That IWS Symphony has been the source of truth for EOD settlement, regulatory extracts, actuarial rollups, and mainframe JCL orchestration for a decade or more. The z/OS calendars, the on-call runbook, the auditor sign-offs — they all point at IWS.

This component is designed for the shape where you're **not** migrating off it. IWS keeps owning the batch that only IWS can own (z/OS JCL wrappers, mainframe scheduling, distributed workstation dependencies, decades of scheduling logic). Dagster owns the cloud / analytics / AI pipeline that has to consume that batch. Both share a single Dagster UI with correct lineage. Multi-year modernization stops being a rip-and-replace bet and becomes an incremental shift.

If you *are* doing a full migration off IWS, the `airflow_dag_proxy` / `sql_transform` / `snowflake_workspace` / warehouse-migration components are the toolkit — this component keeps IWS in the loop.

## What this demonstrates

- **Dagster wraps IWS Jobs.** Materializing an asset submits the Job to IWS with the partition key as scheduled date, polls to terminal state (`Waiting → Ready → Running → Succ`), retrieves the stdlist.
- **IWS can trigger Dagster.** The `iws_inbound_trigger` sensor confirms the wire-up when an IWS job's post-step hits Dagster's GraphQL `launchRun` mutation.
- **Operational ops are Dagster jobs.** Rerun / hold / release / kill / reconcile are runnable from the Dagster UI — one pane of glass across IWS + Dagster.
- **Reconciliation as a scheduled job.** Every hour, compare IWS' plan state to Dagster's materialization ledger and report drift (with `Abend` alerts).

## Asset + job + sensor + schedule graph

```
Assets   ── iws_eod_settlement             [daily partitioned, kinds: python + iws]
         ── iws_regulatory_extract         [daily partitioned, kinds: python + iws]

Jobs     ── iws_rerun_job                  (run config: {job_id})
         ── iws_hold_job                   (run config: {job_id})
         ── iws_release_job                (run config: {job_id})
         ── iws_kill_job                   (run config: {job_id})
         ── iws_reconciliation             (compare IWS state vs Dagster; report drift)

Sensors  ── iws_external_execution_monitor (poll every 60s)
         ── iws_inbound_trigger            (IWS -> Dagster GraphQL)

Schedule ── iws_reconciliation_schedule    (cron: 0 * * * *)
```

## Live output — one partition materialization

Actual run logs (setup script materializes partition `2024-06-01` end-to-end):

```
[AUTH]    Authorization: Basic c3ZjX2RhZ3N0... (HTTP Basic)
[SUBMIT]  POST https://iws.internal:31116/twsd/v1/plan/current/jobstream - DAILY_BATCH/EOD_SETTLE
  Payload: {
    "workstation": "CPU1-MASTER",
    "name": "EOD_SETTLE",
    "application": "DAILY_BATCH",
    "priority": 50,
    "aliasName": "",
    "variables": {}
  }
  Response: 201 Created - jobId: IWS-0B22D35C
[POLL]    GET https://iws.internal:31116/twsd/v1/plan/current/job/IWS-0B22D35C -> status=Waiting
[POLL]    GET https://iws.internal:31116/twsd/v1/plan/current/job/IWS-0B22D35C -> status=Ready
[POLL]    GET https://iws.internal:31116/twsd/v1/plan/current/job/IWS-0B22D35C -> status=Running
[POLL]    GET https://iws.internal:31116/twsd/v1/plan/current/job/IWS-0B22D35C -> status=Running
[POLL]    GET https://iws.internal:31116/twsd/v1/plan/current/job/IWS-0B22D35C -> status=Succ
[STDLIST] GET https://iws.internal:31116/twsd/v1/plan/current/job/IWS-0B22D35C/stdlist -> 487 lines
[DONE]    EOD_SETTLE -> Succ (jobId: IWS-0B22D35C, workstation: CPU1-MASTER)
ASSET_MATERIALIZATION - Materialized value iws_eod.
RUN_SUCCESS
```

Every log line above corresponds to a REST call the component makes against a real IWS instance when `demo_mode: false` — the URL shapes, JSON payload, status transitions, and stdlist retrieval are the actual production code path with the transport swapped for stdout.

## Distributed engine vs z/OS engine — two different REST prefixes

> IWS ships two REST surfaces and the prefix differs between them:
>
> - **Distributed engine** (Linux / AIX / Solaris / Windows workstations): `https://<iws-host>:31116/twsd/v1`
> - **z/OS engine** (mainframe controller): `https://<iws-host>:31116/twsz/v1`
>
> Payload shapes are similar but not identical — the z/OS engine surfaces JCL-specific fields, plan-selector semantics, and workstation types that don't appear on distributed. This component targets the modern JSON REST surface of the distributed engine by default. If you're on z/OS, swap `/twsd/v1` for `/twsz/v1` in `endpoint`. If your version needs a different prefix or plan selector, either bake it into `endpoint` or fork `_execute_iws` in `component.py`. Demo mode is unaffected.

## Docker / eval images

IBM publishes Workload Automation container images under `icr.io/wa-container/*` (e.g. `icr.io/wa-container/wa-server-distr`). These are **BYOL / license-gated** — you need an IBM entitlement key to pull them. That gate is why the component ships `demo_mode: true` as the default: the simulator exposes exactly the REST endpoints the component hits with realistic responses (including the state-machine walk through `Waiting → Ready → Running → Succ`) so evaluators can drive the full Dagster surface **without any IBM entitlements or license conversation**. When you swap in a real IWS instance later, only the `endpoint` + `IWS_USER` / `IWS_PASSWORD` env vars change.

## Terminology cheat-sheet — IWS ↔ Control-M ↔ RunMyJobs

For teams running multiple schedulers (or migrating between them):

| IBM Workload Scheduler | Control-M | RunMyJobs |
|---|---|---|
| Job | Job | JobDefinition |
| Application (JobStream) | Folder | Application |
| Workstation | Agent / Host | Queue |
| scheduled time / IA | ODATE | scheduledTime |
| jobId | runId | processId |
| Succ | Ended OK | Completed |
| Abend | Ended Not OK | Error |
| Waiting / Ready / Held | Waiting / Held | Waiting Time / Ready |
| Cancelled | Cancelled | Killed |

Sister components ship with the same asset / op / sensor / schedule surface — a shop running multiple schedulers can present a single Dagster pane of glass over all of them:

- [`controlm_integration`](controlm_integration.md) — BMC Control-M
- [`runmyjobs_integration`](runmyjobs_integration.md) — Redwood RunMyJobs / SAP Redwood Scheduler
- [`stonebranch_uac_integration`](stonebranch_uac_integration.md) — Stonebranch Universal Automation Center

## Point at a real IWS instance

Set `demo_mode: false` in `defs.yaml` and pick the right engine prefix:

```yaml
attributes:
  demo_mode: false
  # Distributed engine
  endpoint: "https://iws.prod.internal:31116/twsd/v1"
  # or z/OS engine
  # endpoint: "https://iws-zos.prod.internal:31116/twsz/v1"
  ...
```

Then export credentials:

```bash
export IWS_USER=svc_dagster
export IWS_PASSWORD='<your-password>'
```

Auth is HTTP Basic (`Authorization: Basic base64(user:pass)`). If your IWS instance requires SSO / token-based auth, override the `Authorization` header logic in `_iws_headers` inside `component.py`.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_iws_integration_demo.sh | bash
cd iws-demo
uv run dg dev
```

Then in the UI: click a partition (2024-06-01 or later) → **Materialize**. Watch the full simulated REST trace stream into the run logs. Flip `demo_mode: false` + export `IWS_USER` / `IWS_PASSWORD` and the same clicks drive your real IWS instance.

The setup script also materializes partition `2024-06-01` at the end so you see a green run before you even open the UI.

## REST API endpoints exercised

```
POST /plan/current/jobstream                       — submit a job / jobstream
GET  /plan/current/job/{jobId}                     — poll job status
GET  /plan/current/job/{jobId}/stdlist             — job log / stdlist
POST /plan/current/job/{jobId}/action/rerun        — rerun a failed job
POST /plan/current/job/{jobId}/action/hold         — hold a job
POST /plan/current/job/{jobId}/action/release      — release a held job
POST /plan/current/job/{jobId}/action/kill         — kill a running job
GET  /plan/current/job?status=Succ,Abend&limit=200 — list jobs for reconciliation
```

Statuses: `Waiting`, `Ready`, `Held`, `Running`, `Succ`, `Abend`, `Cancelled` (terminal: `Succ`, `Abend`, `Cancelled`).

## See also

- Sister component: [`controlm_integration`](controlm_integration.md) — same shape for BMC Control-M
- Sister component: [`runmyjobs_integration`](runmyjobs_integration.md) — same shape for Redwood RunMyJobs / SAP Redwood Scheduler
- Sister component: [`stonebranch_uac_integration`](stonebranch_uac_integration.md) — same shape for Stonebranch Universal Automation Center
- Component reference: <https://dagster-component-ui.vercel.app/c/iws_integration>
- Walkthrough index: [examples/README.md](README.md)
