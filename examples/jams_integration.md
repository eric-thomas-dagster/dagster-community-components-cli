# JAMS Integration — Fortra JAMS Scheduler jobs as daily-partitioned Dagster assets

**Hybrid deployment for Windows / .NET shops.** JAMS is Windows-native (Windows Server host, SQL Server metadata store, .NET-first API surface) and dominates the batch layer in .NET-heavy enterprises. This component keeps JAMS owning the Windows-centric batch it was designed for (SQL Server Agent wrappers, PowerShell chains, SSIS-adjacent workflows, legacy on-prem scheduling) while Dagster orchestrates the cloud / analytics / AI pipeline that reads the outputs. One Dagster UI, correct lineage across both.

`demo_mode: true` (default) simulates the JAMS REST API on stdout — the whole component runs end-to-end with zero external dependencies. JAMS runs on Windows Server with no public Docker image, so the simulator is the only zero-license way to smoke-test the full surface.

## Components used

| Component | What it does |
|---|---|
| `jams_integration` | Each declared JAMS Job becomes a daily-partitioned Dagster asset with a retry policy. Ships 5 op-backed jobs (restart / hold / release / cancel / reconcile), 2 sensors (external-execution monitor + inbound trigger from JAMS via Dagster GraphQL), and an hourly reconciliation schedule. |

## What this demonstrates

- **Dagster wraps JAMS Jobs.** Materializing an asset submits the JAMS Job with the partition key as `scheduledTime`, polls to terminal state (Queued → Scheduled → Executing → Completed), retrieves the entry log.
- **JAMS can trigger Dagster.** The `jams_inbound_trigger` sensor confirms the wire-up when a JAMS post-step hits Dagster's GraphQL `launchRun` mutation.
- **Operational ops are Dagster jobs.** Restart / hold / release / cancel / reconcile are runnable from the Dagster UI.
- **Reconciliation as a scheduled job.** Every hour, compare JAMS' state to Dagster's materialization ledger and report drift.

## Asset + job + sensor + schedule graph

```
Assets   ── jams_eod_settlement           [daily partitioned, kinds: python + jams]
         ── jams_regulatory_extract       [daily partitioned, kinds: python + jams]
         ── jams_settlement_table         [source, kinds: jams + database]

Jobs     ── jams_restart_entry            (run config: {entry_id})
         ── jams_hold_entry               (run config: {entry_id})
         ── jams_release_entry            (run config: {entry_id})
         ── jams_cancel_entry             (run config: {entry_id})
         ── jams_reconciliation           (compare JAMS state vs Dagster; report drift)

Sensors  ── jams_external_execution_monitor  (poll every 60s)
         ── jams_inbound_trigger             (JAMS -> Dagster GraphQL)

Schedule ── jams_reconciliation_schedule     (cron: 0 * * * *)
```

## Live output — one partition materialization in demo mode

Actual run logs (setup script materializes partition `2024-06-01` end-to-end):

```
jams_eod_settlement - [AUTH]   Authorization: Basic c3ZjX2RhZ3N0... (HTTP Basic)
jams_eod_settlement - [SUBMIT] POST https://jams.internal/jams/rest/api/Jobs/EOD_BATCH_SETTLEMENT/Submit -> entryId=JAMS-B0CF967E
jams_eod_settlement -   Payload: {
  "Parameters": {
    "SCHEDULED_TIME": "2024-06-01",
    "FOLDER": "DAILY_BATCH",
    "AGENT": "WIN-AGT-01",
    "APPLICATION": ""
  }
}
jams_eod_settlement - [POLL]   GET https://jams.internal/jams/rest/api/Entries/JAMS-B0CF967E -> State=Queued
jams_eod_settlement - [POLL]   GET https://jams.internal/jams/rest/api/Entries/JAMS-B0CF967E -> State=Scheduled
jams_eod_settlement - [POLL]   GET https://jams.internal/jams/rest/api/Entries/JAMS-B0CF967E -> State=Executing
jams_eod_settlement - [POLL]   GET https://jams.internal/jams/rest/api/Entries/JAMS-B0CF967E -> State=Executing
jams_eod_settlement - [POLL]   GET https://jams.internal/jams/rest/api/Entries/JAMS-B0CF967E -> State=Completed
jams_eod_settlement - [LOG]    GET https://jams.internal/jams/rest/api/Entries/JAMS-B0CF967E/Log -> 487 lines
jams_eod_settlement - [DONE]   EOD_BATCH_SETTLEMENT -> Completed (entryId: JAMS-B0CF967E, folder: DAILY_BATCH)
ASSET_MATERIALIZATION - Materialized value jams_eod_settlement.
RUN_SUCCESS
```

Every log line reflects the exact REST verb + path the production executor calls when `demo_mode: false`. Flipping to production mode changes the transport, not the shape.

## Why a simulator instead of a Docker image

Fortra JAMS runs on Windows Server with a SQL Server metadata store — it is **not** distributed as a public container image. There is no `docker pull fortra/jams` equivalent to spin up locally. The simulator emits the exact REST surface the production executor targets so the whole Dagster wiring (assets, ops, sensors, schedule, retries, metadata) is exercised without a JAMS license. When you swap in a real JAMS instance, only the `endpoint` + `JAMS_USER` / `JAMS_PASSWORD` env vars change.

Asset metadata captured on every materialization: `external_entry_id`, `state`, `scheduled_time`, `duration_seconds`, `demo_mode`.

## Terminology cheat-sheet — Control-M ↔ RunMyJobs ↔ JAMS

For teams running two or more (or migrating between them):

| Control-M | RunMyJobs | JAMS |
|---|---|---|
| Job | JobDefinition | Job |
| Folder | Application | Folder |
| Agent / Host | Queue | Agent |
| ODATE | scheduledTime | scheduledTime |
| runId | processId | Entry ID |
| "Ended OK" / "Ended Not OK" | "Completed" / "Error" | "Completed" / "Failed" |

Sister components with the exact same asset / op / sensor / schedule surface — a shop running two or three schedulers can present a single Dagster pane of glass over all of them:

- [`activebatch_integration`](activebatch_integration.md) — ActiveBatch (also Windows-native, .NET-heavy shops)
- [`controlm_integration`](controlm_integration.md) — BMC Control-M
- [`runmyjobs_integration`](runmyjobs_integration.md) — Redwood RunMyJobs / SAP Redwood Schedule

## Point at a real JAMS instance

Set `demo_mode: false` in `defs.yaml` and override `endpoint`:

```yaml
attributes:
  demo_mode: false
  endpoint: "https://jams.prod.internal/jams/rest/api"
  ...
```

Then export credentials:

```bash
export JAMS_USER=svc_dagster
export JAMS_PASSWORD='<your-password>'
```

**API-path caveat.** JAMS REST paths vary across versions (6.x vs 7.x REST surfaces differ in prefix and payload shape). The URIs in this component target the modern `/jams/rest/api` JSON surface. If your JAMS instance uses a different prefix (`/api/rest/...` on older builds, or a fully-custom path), either bake it into `endpoint` or fork `_execute_jams` in `component.py` to match. Some deployments also require a token exchange via `POST /Authentication` before Basic auth is accepted — that's a two-line addition to the executor. Demo mode is unaffected.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_jams_integration_demo.sh | bash
cd jams-demo
uv run dg dev
```

Then in the UI: click a partition (2024-06-01 or later) → **Materialize**. Watch the full REST API trace stream into the run logs — every POLL shows the exact REST call the production executor makes.

The setup script also materializes partition `2024-06-01` at the end so you see a green run before you even open the UI.

Cleanup when done: `rm -rf jams-demo`

## REST API endpoints exercised

```
POST /Jobs/{name}/Submit                        — submit a JAMS Job
GET  /Entries/{entryId}                         — poll entry state
GET  /Entries/{entryId}/Log                     — entry log output
POST /Entries/{entryId}/Restart                 — restart a failed entry
POST /Entries/{entryId}/Hold                    — hold an entry
POST /Entries/{entryId}/Release                 — release a held entry
POST /Entries/{entryId}/Cancel                  — cancel a running entry
GET  /Entries?state=Completed,Failed&
     lastRunAfter=1h&pageSize=200               — list entries for reconciliation
POST /Authentication                            — (optional) exchange creds for token
```

State machine: `Queued -> Scheduled -> Executing -> {Completed | Failed | Cancelled | Held}`. Terminal: `Completed | Failed | Cancelled`.

## The hybrid deployment story

JAMS is designed for the shape where you're **not** migrating off it — JAMS keeps owning the Windows-centric batch that only JAMS can own (SQL Server Agent wrappers, PowerShell chains, SSIS-adjacent workflows, decades of scheduled work), Dagster owns the cloud / analytics / AI pipeline, both share a single Dagster UI with correct lineage. Common in .NET-first enterprises where the platform team has heavy Windows-server expertise and no appetite to rip out working scheduling to migrate to a Linux-native tool.

If you *are* doing a full migration off JAMS, the `airflow_dag_proxy` / `sql_transform` / `snowflake_workspace` / warehouse-migration components are the toolkit — this component keeps JAMS in the loop.

## See also

- Sister components (same asset / op / sensor / schedule shape):
  - [`activebatch_integration`](activebatch_integration.md) — ActiveBatch (Windows-native peer)
  - [`controlm_integration`](controlm_integration.md) — BMC Control-M
  - [`runmyjobs_integration`](runmyjobs_integration.md) — Redwood RunMyJobs
- Component reference: <https://dagster-component-ui.vercel.app/c/jams_integration>
- Walkthrough index: [examples/README.md](README.md)
