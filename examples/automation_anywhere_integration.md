# Automation Anywhere Integration — A360 / AAI Control Room bots as daily-partitioned Dagster assets

**RPA meets orchestrated data pipelines.** Automation Anywhere (A360 on-prem or AAI Cloud) keeps owning the RPA layer — the UI-driven screen scraping, PDF form filling, and legacy-app automation that only an RPA platform can do. Dagster wraps each Bot as a daily-partitioned asset, adds `RetryPolicy`, wires operational ops (redeploy / pause / resume / stop / reconcile) into the UI, and gives the whole workflow — RPA plus everything upstream and downstream of it — one lineage graph, one run history, one place to click Materialize.

**Validated end-to-end** — the setup script scaffolds a project, installs the component in `demo_mode: true`, and materializes partition `2024-06-01` against the in-process REST-API simulator. The whole `_execute_automation_anywhere` code shape (AUTH → DEPLOY → POLL × N → LOGS → DONE) runs on stdout with zero external dependencies, so you can wire the pipeline into your Dagster project without any Automation Anywhere entitlement.

## Components used

| Component | What it does |
|---|---|
| `automation_anywhere_integration` | Each declared Bot (`file_id`) becomes a daily-partitioned Dagster asset with a retry policy. Ships 5 op-backed jobs (redeploy / pause / resume / stop / reconcile), 2 sensors (external-execution monitor + inbound trigger via `callbackInfo` → Dagster GraphQL `launchRun`), and an hourly reconciliation schedule. |

## What this demonstrates

- **Dagster wraps Automation Anywhere Bots.** Materializing an asset authenticates to the Control Room (`POST /v1/authentication` → JWT), deploys the bot (`POST /v3/automations/deploy` with `botInput` templated from the partition key), polls to terminal state (`DEPLOYED → QUEUED → RUNNING → COMPLETED`), and retrieves execution logs.
- **Automation Anywhere can trigger Dagster.** The `callbackInfo` field on the deploy payload points at a Dagster webhook / GraphQL `launchRun` mutation; the `automation_anywhere_inbound_trigger` sensor confirms the wire-up.
- **Operational ops are Dagster jobs.** Redeploy / pause / resume / stop / reconcile are runnable from the Dagster UI.
- **Reconciliation as a scheduled job.** Every hour, compare Automation Anywhere's `/v3/activity/list` against Dagster's materialization ledger and report drift by status (`COMPLETED` / `FAILED` / `RUNNING` / `QUEUED`).

## Asset + job + sensor + schedule graph

```
Assets   -- aa_invoice_extract       [daily partitioned, kinds: python + automation-anywhere + rpa]
         -- aa_hr_onboarding         [daily partitioned, kinds: python + automation-anywhere + rpa]
         -- aa_invoice_staging_table [source, kinds: automation-anywhere + database]  (optional)

Jobs     -- automation_anywhere_redeploy_bot          (run config: {file_id, device_pool_id})
         -- automation_anywhere_pause_execution       (run config: {execution_id})
         -- automation_anywhere_resume_execution      (run config: {execution_id})
         -- automation_anywhere_stop_execution        (run config: {execution_id})
         -- automation_anywhere_reconciliation        (compare AA state vs Dagster; report drift)

Sensors  -- automation_anywhere_external_execution_monitor  (poll every 60s)
         -- automation_anywhere_inbound_trigger             (AA callbackInfo -> Dagster GraphQL)

Schedule -- automation_anywhere_reconciliation_schedule     (cron: 0 * * * *)
```

## Live output — one partition materialization in demo mode

Actual run logs from `dg.materialize([aa_invoice], partition_key="2024-06-01")` against the in-process simulator:

```
[AUTH]    POST https://control-room.internal/v1/authentication
  Response: {token: 'ey***', tokenType: 'Bearer'}
[DEPLOY]  POST https://control-room.internal/v3/automations/deploy
  Payload: {"fileId": 12345, "poolIds": [7], "botInput": {"InvoiceDate": "2024-06-01"}}
  Response: {deploymentId: 'a17f33bf-19d9-40d3-8917-f2125fd273b3', executionId: 9296553}
[POLL]    GET https://control-room.internal/v3/activity/execution/9296553 -> status=DEPLOYED
[POLL]    GET https://control-room.internal/v3/activity/execution/9296553 -> status=QUEUED
[POLL]    GET https://control-room.internal/v3/activity/execution/9296553 -> status=RUNNING
[POLL]    GET https://control-room.internal/v3/activity/execution/9296553 -> status=RUNNING
[POLL]    GET https://control-room.internal/v3/activity/execution/9296553 -> status=COMPLETED
[LOGS]    GET https://control-room.internal/v3/activity/execution/9296553/logs -> 412 entries
[DONE]    file_id=12345 -> COMPLETED (executionId: 9296553, workspace: Finance)
ASSET_MATERIALIZATION - Materialized value aa_invoice.
RUN_SUCCESS
```

Every step logged in `demo_mode` matches the shape the real `_execute_automation_anywhere` code path emits — swap in a real Control Room and the log lines look the same, minus the `demo_mode: true` metadata flag.

Asset metadata captured on every materialization: `deployment_id`, `execution_id`, `status`, `partition_key`, `duration_seconds`, `demo_mode`.

## Why no Docker option

Automation Anywhere Control Room is enterprise-licensed and Windows-heavy — there is no public Docker image, and there is no on-prem PoC container the way Kafka / MongoDB / Neo4j ship for local development. Two paths for hitting the real REST surface:

- **Automation Anywhere Cloud (AAI)** at `https://aai.automationanywhere.com/` — AA offers a business trial account; once provisioned, point `endpoint` at your tenant URL, set the two env vars, flip `demo_mode: false`.
- **On-prem A360** — supply `endpoint: "https://<control-room-host>"`, plus the same env vars.

Either way, the demo-mode simulator ships end-to-end zero-license, zero-network, so the pipeline shape (asset graph, retry policy, ops, sensors, reconciliation schedule) is wire-able into your Dagster project without any AA entitlement.

## Terminology cheat-sheet — Control-M vs RunMyJobs vs Automation Anywhere

For teams running multiple orchestrators / RPA platforms:

| Control-M | RunMyJobs | Automation Anywhere |
|---|---|---|
| Job | JobDefinition | Bot (File) |
| Folder | Application | Workspace / Folder |
| Agent / Host | Queue | Device Pool |
| ODATE | scheduledTime | `botInput.run_date` |
| runId | processId | Deployment (executionId) |
| "Ended OK" / "Ended Not OK" | "Completed" / "Error" | "COMPLETED" / "FAILED" |

Sister components with the same asset / op / sensor / schedule surface so a shop running multiple platforms can present a single Dagster pane of glass over all of them:

- **RPA family**: [`uipath_orchestrator_integration`](uipath_orchestrator_integration.md), [`blue_prism_integration`](blue_prism_integration.md), [`power_automate_integration`](power_automate_integration.md)
- **Batch-scheduler family**: [`controlm_integration`](controlm_integration.md), [`runmyjobs_integration`](runmyjobs_integration.md)

## Operational ops — Dagster as the pane of glass

Five ops ship as Dagster jobs, all runnable from the UI with typed run config:

| Job | Config | REST call |
|---|---|---|
| `automation_anywhere_redeploy_bot` | `{file_id, device_pool_id}` | `POST /v3/automations/deploy` |
| `automation_anywhere_pause_execution` | `{execution_id}` | `POST /v3/activity/execution/{id}/pause` |
| `automation_anywhere_resume_execution` | `{execution_id}` | `POST /v3/activity/execution/{id}/resume` |
| `automation_anywhere_stop_execution` | `{execution_id}` | `POST /v3/activity/execution/{id}/stop` |
| `automation_anywhere_reconciliation` | — | `POST /v3/activity/list` (filter: `createdOn >= now-1h`) |

The `automation_anywhere_reconciliation_schedule` runs the reconciliation job every hour (STOPPED by default — toggle in the UI when ready).

## REST API endpoints exercised

```
POST /v1/authentication                        -- obtain JWT (returns {token})
                                                  use as X-Authorization: <token>
POST /v3/automations/deploy                    -- deploy a bot
                                                  body: {fileId, runAsUserIds, poolIds,
                                                         overrideDefaultDevice, callbackInfo,
                                                         botInput}
                                                  returns: {deploymentId, executionId, ...}
GET  /v3/activity/execution/{id}               -- poll execution status
                                                  status in DEPLOYED / SCHEDULED / QUEUED /
                                                           RUNNING / COMPLETED / FAILED /
                                                           CANCELLED / DEPLOY_FAILED /
                                                           TIMED_OUT
GET  /v3/activity/execution/{id}/logs          -- execution logs
POST /v3/activity/execution/{id}/stop          -- stop a running execution
POST /v3/activity/execution/{id}/pause         -- pause a running execution
POST /v3/activity/execution/{id}/resume        -- resume a paused execution
POST /v3/activity/list                         -- list activity (reconciliation)
                                                  body: {filter, sort, page}
```

Terminal execution states: `COMPLETED`, `FAILED`, `CANCELLED`, `DEPLOY_FAILED`, `TIMED_OUT`.

## Point at a real Control Room

Set `demo_mode: false` in `defs.yaml` and override `endpoint`:

```yaml
attributes:
  demo_mode: false
  endpoint: "https://control-room.prod.internal"     # or your AAI tenant URL
  ...
```

Then export credentials:

```bash
export AUTOMATION_ANYWHERE_USER=svc_dagster
export AUTOMATION_ANYWHERE_PASSWORD='<your-password>'   # or apiKey
```

**API-path caveat.** Control Room REST endpoints vary between A360 on-prem versions and AAI Cloud — `v1/authentication` + `v3/activity` is the modern surface, but older on-prem installs use different prefixes or payload shapes. If your instance differs, either bake the prefix into `endpoint` or fork `_execute_automation_anywhere` in `component.py` to match. Demo mode is unaffected.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_automation_anywhere_integration_demo.sh | bash
cd automation-anywhere-demo
uv run dg dev
```

Then in the UI: click a partition (2024-06-01 or later) on `aa_invoice_extract` -> **Materialize**. Watch the full REST-API trace stream into the run logs.

The setup script also materializes partition `2024-06-01` at the end so you see a green run before you even open the UI.

Cleanup when done: `rm -rf automation-anywhere-demo`.

## The hybrid deployment story

Automation Anywhere is designed for the shape where you're **not** migrating off it — AA keeps owning the RPA workload only RPA can own (UI screen scraping, PDF form filling, legacy-app automation), Dagster owns the cloud / analytics / AI pipeline that sandwiches the bot on either side (inbound file drop → RPA extract → dbt / warehouse / ML), both share a single Dagster UI with correct lineage.

Not an Automation Anywhere killer — headless API-first replacements are a better fit if you're doing a full RPA rip-and-replace. This component keeps AA in the loop and adds the surrounding pipeline that AA on its own can't own.

## See also

- Component reference: <https://dagster-component-ui.vercel.app/c/automation_anywhere_integration>
- Sister RPA components: [`uipath_orchestrator_integration`](uipath_orchestrator_integration.md), [`blue_prism_integration`](blue_prism_integration.md), [`power_automate_integration`](power_automate_integration.md)
- Sister batch-scheduler components: [`controlm_integration`](controlm_integration.md), [`runmyjobs_integration`](runmyjobs_integration.md)
- Walkthrough index: [examples/README.md](README.md)
