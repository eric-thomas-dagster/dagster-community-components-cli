# Power Automate Integration — Microsoft cloud flows as daily-partitioned Dagster assets

**M365-native RPA in the data pipeline.** Power Automate cloud flows are pervasive in M365 shops — invoice approvals, HR onboarding, SharePoint / Outlook / Teams orchestration — and they usually live in a totally separate operational pane from the data platform. This component brings them into the Dagster lineage graph: each cloud flow becomes a daily-partitioned Dagster asset, ops for retrigger / cancel / turn-off / turn-on / reconcile ship as runnable Dagster jobs, and a sensor pair wires the two-way trigger story (Dagster fires the flow; the flow's HTTP action fires Dagster back).

**Validated end-to-end** (`demo_mode: true` simulates the full Flow Management REST lifecycle on stdout — zero external dependencies, no M365 tenant needed). Materialize any partition and you see the whole call trace (AUTH → TRIGGER → POLL × 5 → DONE) in the run logs.

## Components used

| Component | What it does |
|---|---|
| `power_automate_integration` | Each declared Power Automate cloud flow becomes a daily-partitioned Dagster asset with a retry policy. Ships 5 op-backed jobs (retrigger / cancel run / turn off / turn on / reconcile), 2 sensors (external-execution monitor + inbound trigger from Power Automate via Dagster GraphQL), and an hourly reconciliation schedule. |

## What this demonstrates

- **Dagster wraps Power Automate cloud flows.** Materializing an asset POSTs to the manual trigger endpoint with the partition key templated into `trigger_input`, polls the run URL to terminal state (`Running` → `Succeeded` / `Failed` / `Cancelled` / `Skipped`), records the `runName`.
- **Power Automate can trigger Dagster.** The `power_automate_inbound_trigger` sensor confirms the wire-up when a cloud flow's HTTP action hits Dagster's GraphQL `launchRun` mutation.
- **Operational ops are Dagster jobs.** Retrigger / cancel / turn off / turn on / reconcile are runnable from the Dagster UI. Kill a runaway invoice-approval flow from the same pane as your dbt runs.
- **Reconciliation as a scheduled job.** Every hour, compare Power Automate's actual run history to Dagster's materialization ledger and report drift.

## Asset + job + sensor + schedule graph

```
Assets   ── pa_invoice_extract          [daily partitioned, kinds: python + power-automate + rpa]
         ── pa_hr_onboarding            [daily partitioned, kinds: python + power-automate + rpa]

Jobs     ── power_automate_retrigger        (run config: {flow_id, environment_id})
         ── power_automate_cancel_run       (run config: {flow_id, environment_id, run_name})
         ── power_automate_turn_off         (run config: {flow_id, environment_id})
         ── power_automate_turn_on          (run config: {flow_id, environment_id})
         ── power_automate_reconciliation   (compare PA run history vs Dagster; report drift)

Sensors  ── power_automate_external_execution_monitor  (poll every 60s)
         ── power_automate_inbound_trigger             (Power Automate -> Dagster GraphQL)

Schedule ── power_automate_reconciliation_schedule     (cron: 0 * * * *)
```

## Live output — one partition materialization in demo mode

Actual run logs (setup script materializes partition `2024-06-01` end-to-end):

```
[AUTH]    POST https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token — Azure AD client credentials
  Response: {access_token: 'ey***', token_type: 'Bearer', expires_in: 3600}
[TRIGGER] POST https://api.flow.microsoft.com/providers/Microsoft.ProcessSimple/environments/Default-xyz789/flows/abc-1234/triggers/manual/run?api-version=2016-11-01
  Payload: {
    "DATE": "2024-06-01"
  }
  Response: 202 Accepted
  Location: https://api.flow.microsoft.com/providers/Microsoft.ProcessSimple/environments/Default-xyz789/flows/abc-1234/runs/08585CE02A2302D564684B6C8CDFF
[POLL]    GET .../runs/08585CE02A2302D564684B6C8CDFF?api-version=2016-11-01 -> status=Running
[POLL]    GET .../runs/08585CE02A2302D564684B6C8CDFF?api-version=2016-11-01 -> status=Running
[POLL]    GET .../runs/08585CE02A2302D564684B6C8CDFF?api-version=2016-11-01 -> status=Running
[POLL]    GET .../runs/08585CE02A2302D564684B6C8CDFF?api-version=2016-11-01 -> status=Running
[POLL]    GET .../runs/08585CE02A2302D564684B6C8CDFF?api-version=2016-11-01 -> status=Succeeded
[DONE]    abc-1234 -> Succeeded (runName: 08585CE02A2302D564684B6C8CDFF, environment: Default-xyz789)
ASSET_MATERIALIZATION - Materialized value pa_invoice.
RUN_SUCCESS
```

Every `{partition_key}` in `trigger_input` values gets substituted before the payload is sent. Asset metadata captured on every materialization: `external_run_name`, `status`, `scheduled_time`, `environment_id`, `duration_seconds`, `demo_mode`.

## Why no Docker

Power Automate is a **Microsoft cloud service exclusively**. There is no on-prem image to spin up; every M365 tenant gets a `Default-<tenant-guid>` Environment automatically, and Power Automate cloud flows are included in most M365 licenses. On-prem RPA is a separate product (Power Automate for desktop + the on-prem data gateway); this component targets **cloud flows**.

The demo simulator exercises the same code paths as the production executor — swapping `demo_mode: true` for `demo_mode: false` plus three env vars is the only change needed to point at a live tenant.

## Terminology cheat-sheet

For teams running Power Automate alongside Control-M / RunMyJobs:

| Control-M      | RunMyJobs        | Power Automate                    |
|---             |---               |---                                |
| Job            | JobDefinition    | Cloud Flow                        |
| Folder         | Application      | Environment                       |
| Agent / Host   | Queue            | (n/a — flows run in MS's cloud)   |
| —              | —                | Solution (business tag)           |
| ODATE          | scheduledTime    | Trigger input variable            |
| runId          | processId        | runName                           |
| "Ended OK"     | "Completed"      | "Succeeded"                       |
| "Ended NOK"    | "Error"          | "Failed"                          |

**Environment IS the runtime context.** Power Automate cloud flows run in Microsoft's infrastructure — there is no per-run "host" concept like a batch scheduler's agent. The Environment (usually `Default-<tenant-guid>`) is where the flow lives and executes; the Solution (Dataverse) is a business-grouping tag around a set of flows, not a runtime.

## Operational ops — Dagster as the pane of glass

Five ops ship as Dagster jobs so you can drive Power Automate cloud flows directly from the Dagster UI:

| Job                              | Config                                | REST call                                    |
|---                               |---                                    |---                                           |
| `power_automate_retrigger`       | `{flow_id, environment_id}`           | `POST /triggers/manual/run`                  |
| `power_automate_cancel_run`      | `{flow_id, environment_id, run_name}` | `POST /runs/{run_name}/cancel`               |
| `power_automate_turn_off`        | `{flow_id, environment_id}`           | `POST /stop`                                 |
| `power_automate_turn_on`         | `{flow_id, environment_id}`           | `POST /start`                                |
| `power_automate_reconciliation`  | —                                     | `GET  /runs?$top=200` → drift summary        |

The `power_automate_reconciliation_schedule` runs the reconciliation job every hour (STOPPED by default — toggle in the UI when ready).

## Point at a real Power Automate service

### 1. Register an Azure AD (Entra ID) app

In the Azure portal: **Entra ID → App registrations → New registration**.

Under **API permissions**, add application permissions for **Power Automate Service** (or **Flow Service**):

- `Flows.Read.All`    — list flows / read run history (used by the reconciliation + monitor)
- `Flows.Manage.All`  — trigger / cancel / turn off / turn on

Grant admin consent for the tenant. Then under **Certificates & secrets** create a client secret (value shown once — copy it).

Direct console URLs:

- App registrations: <https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade>
- Docs — register an app: <https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app>
- Docs — Power Automate Web API auth: <https://learn.microsoft.com/en-us/power-automate/web-api>

### 2. Export the three env vars

```bash
export POWER_AUTOMATE_TENANT_ID='<your-tenant-guid>'
export POWER_AUTOMATE_CLIENT_ID='<your-app-registration-client-id>'
export POWER_AUTOMATE_CLIENT_SECRET='<your-client-secret-value>'
```

The component exchanges these for a Bearer token against `login.microsoftonline.com` on every asset run.

### 3. Flip the switch in `defs.yaml`

```yaml
attributes:
  demo_mode: false
  endpoint: "https://api.flow.microsoft.com"      # commercial cloud
  # endpoint: "https://gov.api.flow.microsoft.us" # GCC-High example
  ...
```

**API-path caveat.** The Flow Management REST surface at `api.flow.microsoft.com` is the modern JSON API used by the Power Automate UI. National-cloud endpoints (GCC / GCC-High / DoD / China / Germany) and the older Dataverse-hosted Power Platform Web API have different hostnames — override `endpoint` accordingly. Demo mode is unaffected.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_power_automate_integration_demo.sh | bash
cd power-automate-demo
uv run dg dev
```

Then in the UI: click a partition (2024-06-01 or later) on `pa_invoice_extract` → **Materialize**. Watch the full Flow Management REST trace stream into the run logs — every POLL simulates a real HTTP GET against the flow's run URL.

The setup script also materializes partition `2024-06-01` at the end so you see a green run before you even open the UI.

## REST API endpoints exercised (Flow Management REST, `api-version=2016-11-01`)

```
POST /providers/Microsoft.ProcessSimple/environments/{env}/flows/{id}/triggers/manual/run
                                                                — trigger a cloud flow
GET  /providers/Microsoft.ProcessSimple/environments/{env}/flows/{id}/runs/{run_name}
                                                                — poll a run's status
GET  /providers/Microsoft.ProcessSimple/environments/{env}/flows/{id}/runs
                                                                — list run history (recon + monitor)
POST /providers/Microsoft.ProcessSimple/environments/{env}/flows/{id}/runs/{run_name}/cancel
                                                                — cancel a running run
POST /providers/Microsoft.ProcessSimple/environments/{env}/flows/{id}/stop
                                                                — turn flow off (disable)
POST /providers/Microsoft.ProcessSimple/environments/{env}/flows/{id}/start
                                                                — turn flow on (enable)

Auth:
POST https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token
     grant_type=client_credentials
     scope=https://service.flow.microsoft.com/.default
     -> Bearer <access_token>
```

## The two-way trigger story

Power Automate and Dagster both need to be able to start work in the other:

**Dagster -> Power Automate** — assets: materializing the asset triggers the flow's manual trigger, polls to terminal state, records `runName` in metadata.

**Power Automate -> Dagster** — the `power_automate_inbound_trigger` sensor confirms the reverse direction. Wire-up in production: the cloud flow uses an HTTP action to call Dagster's GraphQL `launchRun` mutation on completion / on business event. The sensor produces observable ticks in the Dagster UI so you can see the trigger fire.

## The hybrid deployment story

Power Automate keeps owning the RPA / SharePoint / Outlook / Teams orchestration only Power Automate can own; Dagster owns the data / analytics / AI pipeline; both are visible from the same Dagster UI with correct lineage. Not a Power Automate killer — this component keeps Power Automate in the loop as a first-class citizen of the Dagster catalog.

## Companion integrations

**RPA control plane** — one YAML per platform, one Dagster pane of glass for all of them:

- [`uipath_orchestrator_integration`](uipath_orchestrator_integration.md) — UiPath Orchestrator (unattended robots)
- [`automation_anywhere_integration`](automation_anywhere_integration.md) — Automation Anywhere Control Room
- [`blue_prism_integration`](blue_prism_integration.md) — Blue Prism (on-prem)

**Batch schedulers** — same asset / op / sensor / schedule surface for the enterprise batch world:

- [`controlm_integration`](controlm_integration.md) — BMC Control-M
- [`runmyjobs_integration`](runmyjobs_integration.md) — Redwood RunMyJobs / SAP Redwood Schedule

## See also

- Component reference: <https://dagster-component-ui.vercel.app/c/power_automate_integration>
- Walkthrough index: [examples/README.md](README.md)
