# Stonebranch UAC Integration — Universal Automation Center Tasks as daily-partitioned Dagster assets

**Hybrid deployment / modern WLA.** Stonebranch pitches Universal Automation Center as the drop-in replacement for legacy workload automation (Control-M, IWS, CA7). Most shops don't rip-and-replace overnight — they run Stonebranch alongside the incumbent for months or years while workflows migrate. This component keeps Dagster on top of Stonebranch UAC during that coexistence: the same asset / op / sensor / schedule surface as the `controlm_integration` and `runmyjobs_integration` sisters, so one Dagster pane of glass covers whichever scheduler owns the batch — and stays consistent as tasks move from Control-M → Stonebranch.

`demo_mode: true` (the default) simulates the UAC REST API on stdout — zero external dependencies, zero license required. Flip to `false` and point at a real UAC instance when you're ready.

## Components used

| Component | What it does |
|---|---|
| `stonebranch_uac_integration` | Each declared UAC Task becomes a daily-partitioned Dagster asset with a retry policy. Ships 5 op-backed jobs (rerun / hold / release / cancel / reconcile), 2 sensors (external-execution monitor + inbound trigger from UAC via Dagster GraphQL), and an hourly reconciliation schedule. |

## What this demonstrates

- **Dagster wraps Stonebranch UAC Tasks.** Materializing an asset launches the Task via `POST /resources/task/ops-task-launch`, polls the task instance (`Waiting → Queued → Running → Success`), retrieves output, posts a completion event.
- **Stonebranch UAC can trigger Dagster.** The `stonebranch_inbound_trigger` sensor confirms the wire-up when a UAC Task calls Dagster's GraphQL `launchRun` mutation from a post-step.
- **Operational ops are Dagster jobs.** Rerun / hold / release / cancel / reconcile are runnable from the Dagster UI with `sys_id` as run config.
- **Reconciliation as a scheduled job.** Every hour, compare UAC's task-instance state to Dagster's materialization ledger and report drift.

## Asset + job + sensor + schedule graph

```
Assets   ── sb_eod_settlement            [daily partitioned, kinds: python + stonebranch]
         ── sb_regulatory_extract        [daily partitioned, kinds: python + stonebranch]

Jobs     ── stonebranch_rerun_task_instance     (run config: {sys_id})
         ── stonebranch_hold_task_instance      (run config: {sys_id})
         ── stonebranch_release_task_instance   (run config: {sys_id})
         ── stonebranch_cancel_task_instance    (run config: {sys_id})
         ── stonebranch_reconciliation          (compare UAC state vs Dagster; report drift)

Sensors  ── stonebranch_external_execution_monitor  (poll every 60s)
         ── stonebranch_inbound_trigger             (UAC -> Dagster GraphQL)

Schedule ── stonebranch_reconciliation_schedule     (cron: 0 * * * *)
```

## Live output — one partition materialization in demo_mode

Actual run logs (partition `2024-06-01`, one task `EOD_SETTLE` on the `DAILY_BATCH` workflow, agent `ux-agent-01`):

```
[AUTH]    Authorization: Basic b3BzLmFkbWlu... (HTTP Basic)
[LAUNCH]  POST http://localhost:8080/uc/resources/task/ops-task-launch?taskname=EOD_SETTLE
  Payload: {
    "taskname": "EOD_SETTLE",
    "workflow": "DAILY_BATCH",
    "agent": "ux-agent-01",
    "scheduledTime": "2024-06-01",
    "variables": {}
  }
  Response: 200 OK — taskInstanceId: UAC-DE1C09B0, sysId: 2226b89fff8a403aac9bd7ecccb735f5
[POLL]    GET .../resources/taskinstance/2226b89f... -> status=Waiting
[POLL]    GET .../resources/taskinstance/2226b89f... -> status=Queued
[POLL]    GET .../resources/taskinstance/2226b89f... -> status=Running
[POLL]    GET .../resources/taskinstance/2226b89f... -> status=Running
[POLL]    GET .../resources/taskinstance/2226b89f... -> status=Success
[OUTPUT]  GET .../resources/taskinstance/2226b89f.../output -> 512 lines
[DONE]    EOD_SETTLE -> Success (sysId: 2226b89f..., workflow: DAILY_BATCH)
[EVENT]   POST .../resources/taskinstance/2226b89f.../events
ASSET_MATERIALIZATION - Materialized value sb_eod.
STEP_SUCCESS  RUN_SUCCESS
```

Every log line traces the exact HTTP verb + URL + payload the production executor sends when `demo_mode: false`. Asset metadata captured on every materialization: `sys_id`, `task_instance_id`, `status`, `scheduled_time`, `duration_seconds`, `demo_mode`.

## Point at a real Stonebranch UAC instance

Set `demo_mode: false` in `defs.yaml` and override `endpoint`:

```yaml
attributes:
  demo_mode: false
  endpoint: "http://uac.internal:8080/uc"
  ...
```

Then export credentials:

```bash
export STONEBRANCH_USER=ops.admin
export STONEBRANCH_PASSWORD='<your-password>'
```

**REST-path caveat.** UAC REST paths shift across versions — 7.x introduced the modern JSON surface these paths target (`/resources/task/…`, `/resources/taskinstance/…`); older UAC installs still expose XML variants at different prefixes (e.g. `/uc/ws/…`). Bake your version's prefix into `endpoint` or fork `_execute_stonebranch` in `component.py` to match. Demo mode is unaffected.

## Docker image note

Stonebranch publishes a `stonebranch/uac-demo` image for evaluation, but it requires a **trial license file provisioned by Stonebranch support** — no self-serve pull-and-run. That's why this walkthrough ships `demo_mode: true` as the default path: the simulator exercises the same `_execute_stonebranch` interface end-to-end, with zero license and zero external process, so you can validate the entire Dagster surface (assets, ops, sensors, schedule, retries, partitioning, metadata) before any procurement conversation with Stonebranch.

## Two-way triggers (hybrid deployment story)

Batch orchestration during a Control-M → Stonebranch migration is rarely one-directional. Dagster in the middle solves the coexistence problem in both directions:

```
Dagster asset materialize  ──►  POST /resources/task/ops-task-launch  ──►  Stonebranch UAC Task
                                                                                    │
                                                                                    │  (task_instance completes)
                                                                                    ▼
Dagster receives launchRun  ◄──  UAC post-step: POST /graphql (launchRun)  ◄─  UAC completion webhook
```

Same story with Control-M running in parallel — a Control-M Folder can complete, fire a Dagster run, that run in turn launches a UAC Task that reads the same downstream data. Neither scheduler owns the boundary; Dagster is the pane of glass over both.

## Ops shipped

| Job | Config | UAC REST call |
|---|---|---|
| `stonebranch_rerun_task_instance` | `{sys_id}` | `POST /resources/taskinstance/{sys_id}/ops-task-rerun` |
| `stonebranch_hold_task_instance` | `{sys_id}` | `POST /resources/taskinstance/{sys_id}/ops-task-hold` |
| `stonebranch_release_task_instance` | `{sys_id}` | `POST /resources/taskinstance/{sys_id}/ops-task-release` |
| `stonebranch_cancel_task_instance` | `{sys_id}` | `POST /resources/taskinstance/{sys_id}/ops-task-cancel` |
| `stonebranch_reconciliation` | none | `GET /resources/taskinstance/list?status=Success,Failed&lastRunAfter=1h` |

All five sit in the Dagster UI Jobs tab — click, fill in the `sys_id` field, launch.

## Terminology cheat-sheet — Stonebranch ↔ Control-M ↔ RunMyJobs ↔ IWS

For teams running two or three of these side-by-side (typical during migration):

| Stonebranch UAC | Control-M | RunMyJobs | IBM Workload Scheduler |
|---|---|---|---|
| Task | Job | JobDefinition | Job |
| Workflow | Folder | Application | Job Stream |
| Universal Agent | Agent / Host | Queue | Workstation |
| Task Instance (sysId) | runId | processId | Job Instance |
| scheduledTime | ODATE | scheduledTime | Input Arrival Time |
| Success | Ended OK | Completed | SUCC |
| Failed | Ended Not OK | Error | ABEND |

Companion components with the same asset / op / sensor / schedule surface: [`controlm_integration`](controlm_integration.md), [`runmyjobs_integration`](runmyjobs_integration.md), [`iws_integration`](iws_integration.md). Shops running two or three of these in parallel get one Dagster pane of glass over all of them — perfect for the "we're migrating off Control-M, we're 40% onto Stonebranch, some workloads are stuck on IWS" reality.

## REST API endpoints exercised

```
POST /resources/task/ops-task-launch?taskname=…       — launch a UAC Task
GET  /resources/taskinstance/{sys_id}                 — poll task instance status
GET  /resources/taskinstance/{sys_id}/output          — retrieve task instance output
POST /resources/taskinstance/{sys_id}/events          — post completion event
POST /resources/taskinstance/{sys_id}/ops-task-rerun  — rerun task instance
POST /resources/taskinstance/{sys_id}/ops-task-hold   — hold task instance
POST /resources/taskinstance/{sys_id}/ops-task-release — release held task instance
POST /resources/taskinstance/{sys_id}/ops-task-cancel — cancel running task instance
GET  /resources/taskinstance/list?status=…            — list for reconciliation
```

All authenticated with HTTP Basic (`STONEBRANCH_USER:STONEBRANCH_PASSWORD`).

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_stonebranch_uac_integration_demo.sh | bash
cd stonebranch-uac-demo
uv run dg dev
```

Then in the UI: click a partition (2024-06-01 or later) on `sb_eod_settlement` → **Materialize**. Watch the full REST API trace stream into the run logs.

The setup script also materializes partition `2024-06-01` at the end so you see a green run before you even open the UI.

## Companion sister components

- [`controlm_integration`](controlm_integration.md) — BMC Control-M Automation API (Jobs / Folders / Agents / ODATE)
- [`runmyjobs_integration`](runmyjobs_integration.md) — Redwood RunMyJobs REST (JobDefinitions / Applications / Queues)
- [`iws_integration`](iws_integration.md) — IBM Workload Scheduler REST (Jobs / Job Streams / Workstations)

Same asset / op / sensor / schedule shape across all four. Pick whichever your shop already owns; add the others as migration or acquisition brings them in.

## See also

- Component reference: <https://dagster-component-ui.vercel.app/c/stonebranch_uac_integration>
- Walkthrough index: [examples/README.md](README.md)
