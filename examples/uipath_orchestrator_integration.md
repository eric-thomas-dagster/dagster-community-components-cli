# UiPath Orchestrator Integration — RPA meets orchestrated data pipelines

**RPA meets orchestrated data pipelines.** UiPath owns the desktop / attended / unattended RPA layer (SAP GUI screen-scraping, Excel macro chains, Citrix VDI automations, legacy Windows clients). Dagster orchestrates the wider workflow around it — data ingestion upstream, transforms, the downstream RPA step, notification / handoff after. Common shape at enterprises with heavy RPA investment: keep the bots that only bots can run, let Dagster be the pane of glass over everything.

**Validated end-to-end** — the setup script scaffolds a Dagster project, installs the component in `demo_mode: true`, and materializes one partition. The `demo_mode` simulator walks the full REST API lifecycle (OAuth2 token exchange → StartJobs → poll × 5 → OutputArguments) on stdout, so the entire Dagster surface (assets / ops / sensors / schedules / retry policy / lineage) is validated with zero UiPath licensing.

Flip `demo_mode: false` + set `UIPATH_CLIENT_ID` / `UIPATH_CLIENT_SECRET` (free at [cloud.uipath.com/signup](https://cloud.uipath.com/signup) — the Community tier costs $0) to hit a real Orchestrator instance.

## Components used

| Component | What it does |
|---|---|
| `uipath_orchestrator_integration` | Each declared UiPath Release becomes a daily-partitioned Dagster asset with a retry policy. Ships 5 op-backed jobs (restart / soft-stop / kill / disable_schedule / reconcile), 2 sensors (external-execution monitor + inbound trigger from UiPath via Dagster GraphQL), and an hourly reconciliation schedule. |

## What this demonstrates

- **Dagster wraps UiPath Releases.** Materializing an asset does the OAuth2 client-credentials exchange, calls `POST /odata/Jobs/…/StartJobs` with the partition key templated into `InputArguments`, polls `GET /odata/Jobs({id})` to terminal state (`Pending → Running → Successful`), captures `OutputArguments`.
- **UiPath can trigger Dagster.** The `uipath_inbound_trigger` sensor confirms the wire-up when a UiPath post-step hits Dagster's GraphQL `launchRun` mutation.
- **Operational ops are Dagster jobs.** Restart / soft-stop / kill / disable_schedule / reconcile are runnable from the Dagster UI.
- **Reconciliation as a scheduled job.** Every hour, compare Orchestrator's job list to Dagster's materialization ledger and report drift.

## Asset + job + sensor + schedule graph

```
Assets   ── uipath_invoice_extract          [daily partitioned, kinds: python + uipath + rpa]
         ── uipath_hr_onboarding            [daily partitioned, kinds: python + uipath + rpa]

Jobs     ── uipath_restart_job              (run config: {release_key, folder})
         ── uipath_stop_job_soft            (run config: {job_id})
         ── uipath_stop_job_kill            (run config: {job_id})
         ── uipath_disable_schedule         (run config: {schedule_id})
         ── uipath_reconciliation           (compare Orchestrator state vs Dagster; report drift)

Sensors  ── uipath_external_execution_monitor  (poll every 60s)
         ── uipath_inbound_trigger             (UiPath -> Dagster GraphQL)

Schedule ── uipath_reconciliation_schedule     (cron: 0 * * * *)
```

## The wider workflow — where UiPath fits in

```
   ┌──────────────────────────────────────────────────────────────────┐
   │                         Dagster                                  │
   │                                                                  │
   │  upstream           downstream RPA          post-RPA             │
   │  data ingest   ->   (UiPath Release)   ->   transform + notify   │
   │  (SFTP / API)       StartJobs + poll        (warehouse / Slack)  │
   │                                                                  │
   │  Each UiPath Release is ONE daily-partitioned asset in this      │
   │  graph — lineage, retries, run history, alerts all first-class.  │
   └──────────────────────────────────────────────────────────────────┘
                                  │
                Dagster -> UiPath │  UiPath -> Dagster
                POST /identity_/  │  POST https://dagster.cloud/graphql
                     connect/token│  mutation { launchRun(...) }
                POST /odata/Jobs/ │
                     …/StartJobs  │
                GET  /odata/Jobs  │
                     ({id})       │
                                  ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │                    UiPath Orchestrator                           │
   │                                                                  │
   │  Folder: Finance (id 42)                                         │
   │   ├─ Release: invoice_extract   (robots: finance_*)              │
   │   └─ Release: hr_onboarding     (robots: hr_*)                   │
   └──────────────────────────────────────────────────────────────────┘
```

## Live output — one partition materialization

Actual run logs (setup script materializes partition `2024-06-01` end-to-end against the demo-mode simulator):

```
[AUTH]    POST https://cloud.uipath.com/organization/tenant/orchestrator_/identity_/connect/token — OAuth2 client credentials
  Response: {access_token: 'ey***', token_type: 'Bearer', expires_in: 3600}
[FOLDER]  X-UIPATH-OrganizationUnitId: 42
[START]   POST https://cloud.uipath.com/organization/tenant/orchestrator_/odata/Jobs/UiPath.Server.Configuration.OData.StartJobs
  Payload: {startInfo: {"ReleaseKey": "abc-123-def", "Strategy": "ModernJobsCount", "RobotIds": [], "NoOfRobots": 0, "JobsCount": 1, "InputArguments": "{\"InvoiceDate\": \"2024-06-01\"}"}}
  Response: {value: [{Id: 7467038, Key: '009acc78-…', State: 'Pending'}]}
[POLL]    GET .../odata/Jobs(7467038) -> State=Pending
[POLL]    GET .../odata/Jobs(7467038) -> State=Pending
[POLL]    GET .../odata/Jobs(7467038) -> State=Running
[POLL]    GET .../odata/Jobs(7467038) -> State=Running
[POLL]    GET .../odata/Jobs(7467038) -> State=Successful
[OUTPUT]  53 chars in OutputArguments
[DONE]    abc-123-def -> Successful (Id: 7467038, folder: Finance)
ASSET_MATERIALIZATION - Materialized value uipath_invoice.
RUN_SUCCESS
```

Every log line is the exact shape a real Orchestrator call produces — same OData paths, same `startInfo` payload, same `State` transitions. When you flip `demo_mode: false`, the trace prefix changes from simulator to real HTTP but the shape is identical.

Asset metadata captured on every materialization: `external_job_id`, `external_job_key`, `status`, `scheduled_time`, `output_arguments_chars`, `duration_seconds`, `demo_mode`.

## Why no Docker option

UiPath does not publish a public Orchestrator container image. Orchestrator is a Windows-native (or enterprise-licensed Linux) server product, sold per node. That is why the demo path is the in-process stdout simulator — you validate the full component wiring (assets / ops / sensors / schedules / retry policy / lineage) with zero infrastructure and zero license.

For a real endpoint, the fastest option is the UiPath **Cloud Community** tier — free at [cloud.uipath.com/signup](https://cloud.uipath.com/signup), no purchase required. Register an org + tenant, create an External Application under **Admin -> External Applications**, grab the client_id / client_secret, and you have a real Orchestrator to point at.

## Terminology cheat-sheet — UiPath ↔ Control-M ↔ RunMyJobs

For teams running any combination of these (or migrating between them):

| UiPath Orchestrator | Control-M | RunMyJobs |
|---|---|---|
| Release (Process) | Job | JobDefinition |
| Folder | Folder | Application |
| Robot / Machine | Agent / Host | Queue |
| Job.Key | runId | processId |
| InputArguments | ODATE | scheduledTime |
| "Successful" | "Ended OK" | "Completed" |
| "Faulted" | "Ended Not OK" | "Error" |

Sister components [`controlm_integration`](controlm_integration.md) and [`runmyjobs_integration`](runmyjobs_integration.md) have the same asset / op / sensor / schedule shape, so a shop running RPA (UiPath) alongside batch (Control-M or RMJ) can present a single Dagster pane of glass over the whole automation stack.

## Operational ops — Dagster as the RPA pane of glass

Five ops ship as Dagster jobs so you can drive Orchestrator from the Dagster UI (or Dagster+ Automations):

| Job | Run config | Purpose | OData call |
|---|---|---|---|
| `uipath_restart_job` | `{release_key, folder}` | Start a new job for a Release | `POST /odata/Jobs/…/StartJobs` |
| `uipath_stop_job_soft` | `{job_id}` | SoftStop a running job | `POST /odata/Jobs({id})/…/StopJob` `{strategy: SoftStop}` |
| `uipath_stop_job_kill` | `{job_id}` | Kill a running job | `POST /odata/Jobs({id})/…/StopJob` `{strategy: Kill}` |
| `uipath_disable_schedule` | `{schedule_id}` | Disable a ProcessSchedule | `POST /odata/ProcessSchedules({id})/…/SetEnabled` `{enabled: false}` |
| `uipath_reconciliation` | — | Drift report: Orchestrator state vs Dagster ledger | `GET /odata/Jobs?$top=200&$orderby=StartTime desc` |

The `uipath_reconciliation_schedule` runs the drift report every hour (STOPPED by default — toggle in the UI when ready).

## Point at a real UiPath Orchestrator instance

1. Create an org + tenant at [cloud.uipath.com/signup](https://cloud.uipath.com/signup) — the Community tier is free, no card required — or use your existing Automation Cloud / on-prem Orchestrator.
2. In the Orchestrator UI: **Admin -> External Applications -> Add Application**. Register a **Confidential** application. Grant the scopes `OR.Jobs`, `OR.Execution`, `OR.Folders`. Copy the `App ID` (client_id) and `App Secret` (client_secret).
3. Set `demo_mode: false` in `defs.yaml` and override `endpoint` to your Orchestrator base URL:

```yaml
attributes:
  demo_mode: false
  endpoint: "https://cloud.uipath.com/myorg/mytenant/orchestrator_"
  # on-prem:
  # endpoint: "https://orchestrator.internal"
```

4. Export the credentials:

```bash
export UIPATH_CLIENT_ID='<app-id>'
export UIPATH_CLIENT_SECRET='<app-secret>'
```

**API-surface caveat.** UiPath REST paths and payload shapes vary across Automation Cloud, standalone on-prem Orchestrator, and Orchestrator versions. The URIs in this component target the modern OData surface documented in the [UiPath Orchestrator API reference](https://docs.uipath.com/orchestrator/reference/api-references). If your instance uses a different prefix, either bake it into `endpoint` or fork `_execute_uipath` in `component.py` to match. Demo mode is unaffected.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_uipath_orchestrator_integration_demo.sh | bash
cd uipath-orchestrator-demo
uv run dg dev
```

Then in the UI: click a partition (`2024-06-01` or later) on `uipath_invoice_extract` → **Materialize**. Watch the full REST API trace stream into the run logs — AUTH → FOLDER → START → POLL × 5 → OUTPUT → DONE.

The setup script also materializes partition `2024-06-01` at the end so you see a green run before you even open the UI.

Cleanup when done: `rm -rf uipath-orchestrator-demo`

## REST API endpoints exercised

```
POST /identity_/connect/token                                — OAuth2 client-credentials -> Bearer token
POST /odata/Jobs/UiPath.Server.Configuration.OData.StartJobs — start a Process (Release)
GET  /odata/Jobs({id})                                       — poll job status
POST /odata/Jobs({id})/UiPath.Server.Configuration.OData.StopJob
                                                             — SoftStop or Kill
POST /odata/ProcessSchedules({id})/UiPath.Server.Configuration.OData.SetEnabled
                                                             — enable / disable a schedule
GET  /odata/Jobs?$filter=State eq 'Successful'&$top=20       — external-execution monitor
GET  /odata/Jobs?$top=200&$orderby=StartTime desc            — reconciliation drift report

Header: Authorization: Bearer <token>
Header: X-UIPATH-OrganizationUnitId: <folder_id>   (when targeting a folder)
```

## Companion components

**RPA family** — same asset / op / sensor / schedule shape, different vendor:

- [`automation_anywhere_integration`](automation_anywhere_integration.md) — Automation Anywhere Control Room
- [`blue_prism_integration`](blue_prism_integration.md) — Blue Prism Control Room
- [`power_automate_integration`](power_automate_integration.md) — Microsoft Power Automate cloud flows

**Batch-scheduler family** — for shops where RPA and enterprise batch coexist:

- [`controlm_integration`](controlm_integration.md) — BMC Control-M
- [`runmyjobs_integration`](runmyjobs_integration.md) — Redwood RunMyJobs

Pair an RPA integration with a batch integration when the wider workflow has both — Dagster becomes the single pane of glass over both automation stacks with correct lineage.

## See also

- Component reference: <https://dagster-component-ui.vercel.app/c/uipath_orchestrator_integration>
- Walkthrough index: [examples/README.md](README.md)
