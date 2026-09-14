# Blue Prism Integration — SS&C Blue Prism processes as daily-partitioned Dagster assets

**RPA meets orchestrated data pipelines.** Blue Prism owns process sessions on Runtime Resources (UI automation, credential vaulting, Citrix / thick-client scraping inside a controlled Windows fleet). Dagster owns the wider workflow — upstream feeds, downstream transforms, freshness policies, lineage, and the operational pane of glass. Each declared Blue Prism process becomes a **daily-partitioned Dagster asset** with a retry policy, and 5 operational ops (restart / stop / terminate / hold / reconcile) ship as Dagster jobs so operators drive the bots from the same UI they use for everything else.

**Validated end-to-end** — the setup script scaffolds a Dagster project, installs the component with `demo_mode: true`, and materializes partition `2024-06-01` for `bp_invoice_extract`. The demo simulator walks the whole Blue Prism 7 Web API lifecycle on stdout (AUTH → START → POLL × 5 → LOGS → DONE) with no external dependencies.

## Components used

| Component | What it does |
|---|---|
| `blue_prism_integration` | Each declared Blue Prism process becomes a daily-partitioned Dagster asset with a retry policy. Ships 5 op-backed jobs (restart / stop / terminate / hold / reconcile), 2 sensors (external-execution monitor + inbound trigger from Blue Prism via Dagster GraphQL), and an hourly reconciliation schedule. |

## What this demonstrates

- **Dagster wraps Blue Prism processes.** Materializing an asset authenticates against `/api/v7/auth/authenticate`, starts a session with `/api/v7/sessions` (partition_key templated into inputs), polls to terminal state (Pending → Running → Completed / Failed / Terminated / Stopped), retrieves logs.
- **Blue Prism can trigger Dagster.** The `blue_prism_inbound_trigger` sensor confirms the wire-up when a Blue Prism process's finish stage calls Dagster's GraphQL `launchRun` mutation.
- **Operational ops are Dagster jobs.** Restart / stop / terminate / hold / reconcile are runnable from the Dagster UI — no separate Blue Prism control-room login needed for day-to-day operators.
- **Reconciliation as a scheduled job.** Every hour, compare Blue Prism session state to Dagster's materialization ledger and surface drift + failed sessions.

## Asset + job + sensor + schedule graph

```
Assets   ── bp_invoice_extract           [daily partitioned, kinds: python + blue-prism + rpa]
         ── bp_hr_onboarding             [daily partitioned, kinds: python + blue-prism + rpa]

Jobs     ── blue_prism_restart_session   (run config: {process_id, resource_id})
         ── blue_prism_stop_session      (run config: {session_id})       — soft stop
         ── blue_prism_terminate_session (run config: {session_id})       — hard kill
         ── blue_prism_hold_process      (run config: {process_id})       — disable
         ── blue_prism_reconciliation    (compare Blue Prism vs Dagster; report drift)

Sensors  ── blue_prism_external_execution_monitor  (poll every 60s)
         ── blue_prism_inbound_trigger             (Blue Prism -> Dagster GraphQL)

Schedule ── blue_prism_reconciliation_schedule     (cron: 0 * * * *)
```

## Live output — one partition materialization in demo mode

Actual run logs (setup script materializes partition `2024-06-01` end-to-end):

```
[AUTH]    POST https://blueprism.internal/api/v7/auth/authenticate — Basic (Blue Prism 7 Web API)
  Response: {accessToken: 'ey***', tokenType: 'Bearer'}
[START]   POST https://blueprism.internal/api/v7/sessions
  Payload: {
    "processId": "b1e2c3d4-5f6a-7b8c-9d0e-1f2a3b4c5d6e",
    "resourceId": "a0b1c2d3-4e5f-6a7b-8c9d-0e1f2a3b4c5d",
    "inputs": {
      "InvoiceDate": "2024-06-01"
    }
  }
  Response: {sessionId: 'ae4beb57-b9fd-4fe3-82bf-e5e58e9f97fd', status: 'Pending'}
[POLL]    GET .../sessions/ae4beb57-...      → status=Pending
[POLL]    GET .../sessions/ae4beb57-...      → status=Pending
[POLL]    GET .../sessions/ae4beb57-...      → status=Running
[POLL]    GET .../sessions/ae4beb57-...      → status=Running
[POLL]    GET .../sessions/ae4beb57-...      → status=Completed
[LOGS]    GET .../sessions/ae4beb57-.../logs → 428 entries
[DONE]    b1e2c3d4-... → Completed (sessionId: ae4beb57-..., resource: a0b1c2d3-...)
ASSET_MATERIALIZATION - Materialized value bp_invoice_extract.
RUN_SUCCESS
```

Every URL, payload, and status transition matches the shape of the real Blue Prism 7 Web API. Flip `demo_mode: false` and the exact same call graph fires as live HTTPS against your Blue Prism instance.

## Why demo_mode instead of Docker

Blue Prism does **not** publish a public Docker image — the product is Windows-native, enterprise-licensed to SS&C customers, and the Blue Prism Cloud edition is SaaS-only for existing customers. There is no zero-license way to stand up a throwaway Blue Prism environment. `demo_mode: true` ships a stdout simulator that walks the exact REST surface the production code path hits, so you validate the Dagster shape end-to-end before pointing at a real environment.

Asset metadata captured on every materialization: `external_session_id`, `status`, `scheduled_time`, `duration_seconds`, `demo_mode`, plus the resource + application tags.

## Point at a real Blue Prism instance

Set `demo_mode: false` in `defs.yaml` and override `endpoint`:

```yaml
attributes:
  demo_mode: false
  endpoint: "https://blueprism.prod.internal/api/v7"
  ...
```

Then export credentials — Basic auth (username + password → bearer token) is the default path:

```bash
export BLUE_PRISM_USER=svc_dagster
export BLUE_PRISM_PASSWORD='<your-password>'
```

Or supply an X-API-Key from the Blue Prism 7 Web API config:

```bash
export BLUE_PRISM_API_KEY='<your-web-api-key>'
```

**Blue Prism 6 vs 7 caveat.** This component targets the Blue Prism 7+ REST **Web API**. Older Blue Prism deployments (v6 and earlier) may only expose the legacy **SOAP** interface — the REST paths in this component will 404 against those environments. Verify against your Blue Prism version's Web API reference before flipping `demo_mode` off. The simulator is unaffected either way.

## Terminology cheat-sheet — Control-M ↔ RunMyJobs ↔ Blue Prism

For teams running multiple schedulers + RPA platforms (or migrating between them):

| Control-M | RunMyJobs | Blue Prism |
|---|---|---|
| Job | JobDefinition | Process |
| Folder | Application | Environment / Application |
| Agent / Host | Queue | Runtime Resource |
| runId | processId | sessionId |
| ODATE | scheduledTime | startedAt |
| "Ended OK" / "Ended Not OK" | "Completed" / "Error" | "Completed" / "Failed" / "Terminated" / "Stopped" |

## Operational ops — Dagster as the pane of glass

Five ops ship as Dagster jobs so operators restart / stop / terminate / hold / reconcile Blue Prism sessions from the same UI they use for the wider pipeline:

| Job | Config | REST call | Purpose |
|---|---|---|---|
| `blue_prism_restart_session` | `{process_id, resource_id}` | `POST /api/v7/sessions` | Start a fresh session for a process on a runtime resource |
| `blue_prism_stop_session` | `{session_id}` | `POST /api/v7/sessions/{id}/stop` | Soft-stop a running session |
| `blue_prism_terminate_session` | `{session_id}` | `POST /api/v7/sessions/{id}/terminate` | Hard-terminate a running session |
| `blue_prism_hold_process` | `{process_id}` | `POST /api/v7/processes/{id}/setEnabled` | Disable the process (`{enabled: false}`) |
| `blue_prism_reconciliation` | — | `GET /api/v7/sessions?status=...&startedAfter=...` | Compare Blue Prism state vs Dagster; report drift + Failed sessions |

The `blue_prism_reconciliation_schedule` runs the reconciliation job every hour (STOPPED by default — toggle in the UI when you're ready).

## REST API endpoints exercised

```
POST /api/v7/auth/authenticate                  — Basic auth -> {accessToken}
POST /api/v7/sessions                           — start a session on a resource
GET  /api/v7/sessions/{sessionId}               — poll session status
GET  /api/v7/sessions/{sessionId}/logs          — session log entries
POST /api/v7/sessions/{sessionId}/stop          — soft stop
POST /api/v7/sessions/{sessionId}/terminate     — hard terminate
POST /api/v7/processes/{processId}/setEnabled   — hold / release ({enabled: bool})
GET  /api/v7/processes                          — list registered processes
GET  /api/v7/sessions?status=Completed,Failed&startedAfter=<iso>&limit=200
                                                — list sessions for reconciliation
```

Session status values: `Pending / Running / Terminated / Stopped / Completed / Failed`. Terminal states: `Completed / Failed / Terminated / Stopped`.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_blue_prism_integration_demo.sh | bash
cd blue-prism-demo
uv run dg dev
```

Then in the UI: click a partition (2024-06-01 or later) → **Materialize**. Watch the full REST API trace stream into the run logs.

The setup script also materializes partition `2024-06-01` at the end so you see a green run before you even open the UI.

Cleanup when done: `rm -rf blue-prism-demo`

## The hybrid deployment story

Designed for the **hybrid** shape — Blue Prism keeps owning the RPA work only it can own (UI automation on legacy apps, Citrix / thick-client scraping, credential vaulting inside a controlled Windows fleet), Dagster owns the cloud / analytics / AI pipeline, both share a single Dagster UI with correct lineage.

Not a Blue Prism killer. If you're standardizing on Dagster and can pull the RPA workload apart, prefer native browser automation (`playwright_component` / `httpx` calls / SDK integrations) over screen scraping — this component keeps Blue Prism in the loop until you're ready.

## Sister components

Same integration shape, different vendor — a shop running multiple RPA platforms + batch schedulers can present a single Dagster pane of glass over all of them by declaring one component per vendor:

**RPA platforms:**
- [`uipath_orchestrator_integration`](uipath_orchestrator_integration.md) — UiPath Orchestrator REST API
- [`automation_anywhere_integration`](automation_anywhere_integration.md) — Automation Anywhere Control Room
- [`power_automate_integration`](power_automate_integration.md) — Microsoft Power Automate (Cloud + Desktop)

**Batch schedulers:**
- [`controlm_integration`](controlm_integration.md) — BMC Control-M Automation API
- [`runmyjobs_integration`](runmyjobs_integration.md) — Redwood RunMyJobs REST API

## See also

- Component reference: <https://dagster-component-ui.vercel.app/c/blue_prism_integration>
- Walkthrough index: [examples/README.md](README.md)
