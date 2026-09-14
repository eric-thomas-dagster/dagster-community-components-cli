# ActiveBatch Integration — Redwood ActiveBatch jobs as daily-partitioned Dagster assets

**Validated end-to-end** — the setup script scaffolds a Dagster project, installs `activebatch_integration` in `demo_mode: true`, and materializes one partition against the in-process simulator. The simulator exercises the same asset / op / sensor / schedule surface a real ActiveBatch server would; flip `demo_mode: false` + set `ACTIVEBATCH_USER` / `ACTIVEBATCH_PASSWORD` and the identical YAML hits your ActiveBatch REST endpoint.

ActiveBatch and RunMyJobs are **both Redwood products now** — ActiveBatch is the Windows-native sibling to RMJ. If you're running one, this walkthrough's sister — [`runmyjobs_integration`](runmyjobs_integration.md) — covers the other with the same shape.

## Components used

| Component | What it does |
|---|---|
| `activebatch_integration` | Each declared ActiveBatch Job (by numeric `object_id`) becomes a daily-partitioned Dagster asset with a retry policy. Ships 4 op-backed jobs (restart / hold / release / abort), a reconciliation job + hourly schedule, and 2 sensors (external-execution monitor + inbound trigger from ActiveBatch via Dagster GraphQL). |

## What this demonstrates

- **Dagster wraps ActiveBatch Jobs.** Materializing an asset triggers the Job via `POST /Objects/{objectId}/Triggers` with the partition key as `scheduledDate`, polls `/Instances/{instanceId}` through `Queued -> Running -> Succeeded`, retrieves the instance log, and posts a completion event.
- **ActiveBatch can trigger Dagster.** The `activebatch_inbound_trigger` sensor confirms the wire-up when an ActiveBatch job's post-step hits Dagster's GraphQL `launchRun` mutation.
- **Operational ops are Dagster jobs.** Restart / hold / release / abort / reconcile are runnable from the Dagster UI — Dagster becomes the pane of glass over the ActiveBatch fleet.
- **Reconciliation as a scheduled job.** Every hour, compare ActiveBatch's instance state to Dagster's materialization ledger and report drift.

## Asset + job + sensor + schedule graph

```
Assets   ── ab_eod_settlement                [daily partitioned, kinds: python + activebatch]
         ── ab_regulatory_extract            [daily partitioned, kinds: python + activebatch]
         ── ab_settlement_table              [source, kinds: activebatch + database]  (optional)

Jobs     ── activebatch_restart_instance     (run config: {instance_id})
         ── activebatch_hold_instance        (run config: {instance_id})
         ── activebatch_release_instance     (run config: {instance_id})
         ── activebatch_abort_instance       (run config: {instance_id})
         ── activebatch_reconciliation       (compare AB state vs Dagster; report drift)

Sensors  ── activebatch_external_execution_monitor  (poll every 60s)
         ── activebatch_inbound_trigger             (ActiveBatch -> Dagster GraphQL)

Schedule ── activebatch_reconciliation_schedule     (cron: 0 * * * *)
```

## Live output — one partition materialization (demo_mode)

Actual run logs (setup script materializes partition `2024-06-01` end-to-end):

```
[AUTH]    Authorization: Basic c3ZjX2RhZ3N0... (HTTP Basic)
[TRIGGER] POST http://activebatch.internal/absvc/api/v1/Objects/12345/Triggers -> 12345
  Payload: {
    "arguments": {
      "scheduledDate": "2024-06-01",
      "plan": "DAILY_BATCH",
      "executionQueue": "WIN-Q-01"
    },
    "priority": 5
  }
  Response: 201 Created — instanceId: AB-5C1802FD
[POLL]    GET .../Instances/AB-5C1802FD -> state=Queued
[POLL]    GET .../Instances/AB-5C1802FD -> state=Queued
[POLL]    GET .../Instances/AB-5C1802FD -> state=Running
[POLL]    GET .../Instances/AB-5C1802FD -> state=Running
[POLL]    GET .../Instances/AB-5C1802FD -> state=Succeeded
[LOG]     GET .../Instances/AB-5C1802FD/Log -> 487 lines
[DONE]    12345 -> Succeeded (instanceId: AB-5C1802FD, plan: DAILY_BATCH)
[EVENT]   POST .../Instances/AB-5C1802FD/Events
  Payload: {
    "eventName": "DAGSTER_JOB_COMPLETE",
    "objectId": "12345",
    "instanceId": "AB-5C1802FD",
    "state": "Succeeded",
    "scheduledDate": "2024-06-01",
    "dagsterRunId": "d3117fd7-..."
  }
ASSET_MATERIALIZATION - Materialized value ab_eod_settlement.
RUN_SUCCESS
```

Every log line is what the production code path emits — `demo_mode: true` short-circuits only the actual HTTP `requests` calls. The state machine (Queued → Queued → Running → Running → Succeeded) matches what the real polling loop walks against a live ActiveBatch instance.

Asset metadata captured on every materialization: `external_instance_id`, `state`, `scheduled_date`, `duration_seconds`, `demo_mode`.

## Why no Docker image

**ActiveBatch is Windows-native.** The scheduler runs on Windows Server against a SQL Server backend; Redwood does not publish public Docker images (the pre-Redwood Advanced Systems Concepts distribution didn't either). Every ActiveBatch install is a licensed on-prem Windows host — there's no zero-license eval you can `docker run`.

The `demo_mode: true` simulator ships the whole component end-to-end with zero external dependencies. When you swap in a real ActiveBatch instance, only `demo_mode`, `endpoint`, and the `ACTIVEBATCH_USER` / `ACTIVEBATCH_PASSWORD` env vars change — the asset / op / sensor / schedule surface is identical.

## Terminology cheat-sheet — ActiveBatch ↔ Control-M ↔ RunMyJobs

For teams running more than one of these (or migrating between them):

| ActiveBatch | Control-M | RunMyJobs |
|---|---|---|
| Job (numeric `objectId`) | Job | JobDefinition |
| Plan | Folder | Application |
| Execution Queue | Agent / Host | Queue |
| `scheduledDate` argument | ODATE | `scheduledTime` |
| `instanceId` | runId | processId |
| `Succeeded` | "Ended OK" | "Completed" |
| `Failed` | "Ended Not OK" | "Error" |

The sister components ([`controlm_integration`](controlm_integration.md), [`runmyjobs_integration`](runmyjobs_integration.md)) present the same asset / op / sensor / schedule surface, so a shop running any combination gets a single Dagster pane of glass over every scheduler.

The Windows-heavy shop overlap: **`activebatch_integration`** and the JAMS-equivalent share the same use case (Windows/.NET/SQL Server/SSIS batch orchestration) — pick the one that matches the tool you already have licensed.

## Point at a real ActiveBatch instance

Set `demo_mode: false` in `defs.yaml` and override `endpoint`:

```yaml
attributes:
  demo_mode: false
  endpoint: "http://activebatch.prod.internal/absvc/api/v1"
  ...
```

Then export credentials:

```bash
export ACTIVEBATCH_USER=svc_dagster
export ACTIVEBATCH_PASSWORD='<your-password>'
```

**API-path caveat.** ActiveBatch REST paths shift across versions (v11 / v12 / v13 shipped meaningful surface changes; the pre-Redwood ABAT REST layer differs from the modern `absvc` surface). The URIs in this component target the modern JSON REST surface (`/absvc/api/v1/...`). If your instance uses a different prefix (e.g. `/absvc/api/v2/…` or the older `/ABatSvc/rest/…`), either bake the prefix into `endpoint` or fork `_execute_activebatch` in `component.py` to match. Windows-integrated authentication is supported by ActiveBatch — this component uses HTTP Basic for portability; swap `_activebatch_headers` for `requests_negotiate_sspi.HttpNegotiateAuth` if you need SSPI / Kerberos. Demo mode is unaffected.

## The hybrid deployment story — for Windows-heavy shops

ActiveBatch's home turf is **Windows / .NET / SQL Server / SSIS batch orchestration**. Decades of scheduled work — SSIS packages, Windows services, SQL Agent proxies, ERP job chains, file-drop-triggered COM+ processes — lives in ActiveBatch because that's the tool that speaks native Windows. Rewriting it for a Linux-first orchestrator is a multi-year project that never finishes.

The right shape is **hybrid**:

- **ActiveBatch owns the Windows-native batch** — SSIS, SQL Agent, Windows services, legacy COM, file-drop triggers, ERP job chains. Nothing changes.
- **Dagster owns the cloud / analytics / AI pipeline** — S3 / Snowflake / BigQuery ingest, DataFrame transforms, dbt, embedding / vector / LLM workflows, catalog lineage.
- **Both sides trigger each other.** Dagster materializes an asset → ActiveBatch triggers the underlying Windows job → Dagster observes completion + updates lineage. Or: an ActiveBatch job finishes on Windows → its post-step hits Dagster's GraphQL `launchRun` → downstream cloud pipeline runs.
- **One UI.** The Dagster catalog shows lineage across both worlds — cloud-native assets AND their Windows-scheduler-owned dependencies — so the analytics team sees the whole graph without ever logging in to ActiveBatch.

The component isn't an ActiveBatch killer. If you *are* doing a full migration off ActiveBatch, `airflow_dag_proxy` / `sql_transform` / `snowflake_workspace` / warehouse-migration components are the toolkit — but for most Windows-heavy shops, hybrid beats migration every time.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_activebatch_integration_demo.sh | bash
cd activebatch-demo
uv run dg dev
```

Then in the UI: click a partition (2024-06-01 or later) → **Materialize**. Watch the full REST call trace stream into the run logs — AUTH → TRIGGER → POLL × 5 → LOG → DONE → EVENT.

The setup script also materializes partition `2024-06-01` at the end so you see a green run before you even open the UI.

Cleanup when done: `rm -rf activebatch-demo`

## REST API endpoints exercised

```
POST /Objects/{objectId}/Triggers                            — trigger a Job (returns instanceId)
GET  /Instances/{instanceId}                                 — poll instance state
GET  /Instances/{instanceId}/Log                             — retrieve instance log
POST /Instances/{instanceId}/Events                          — post feedback event
POST /Instances/{instanceId}/Restart                         — restart an instance
POST /Instances/{instanceId}/Hold                            — place instance on hold
POST /Instances/{instanceId}/Release                         — release a held instance
POST /Instances/{instanceId}/Abort                           — abort a running instance
GET  /Instances?filter=state:Succeeded,Failed&recent=1h      — list for reconciliation
```

All requests carry `Authorization: Basic <base64(user:password)>`.

## See also

- Sister components: [`runmyjobs_integration`](runmyjobs_integration.md) (Redwood RunMyJobs — same Redwood family), [`controlm_integration`](controlm_integration.md) (BMC Control-M — mainframe-heavy sibling), `jams_integration` (JAMS — the other Windows-heavy scheduler)
- Component reference: <https://dagster-component-ui.vercel.app/c/activebatch_integration>
- Walkthrough index: [examples/README.md](README.md)
