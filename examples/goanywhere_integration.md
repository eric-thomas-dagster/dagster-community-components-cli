# GoAnywhere Integration — Fortra GoAnywhere MFT projects as daily-partitioned Dagster assets

**Hybrid deployment for MFT-centric shops.** GoAnywhere MFT is Fortra's managed file transfer platform — partner EDI pickup, encrypted SFTP/AS2/PGP transfers, compliance-grade audit trails. This component keeps GoAnywhere owning the file-transfer-centric batch it was designed for while Dagster orchestrates the cloud / analytics / AI pipeline that reads the landed files. One Dagster UI, correct lineage across both.

`demo_mode: true` (default) simulates the GACMD REST API on stdout — the whole component runs end-to-end with zero external dependencies. GoAnywhere MFT does ship real Docker images (`fortrallc/goanywhere-mft`, `fortrallc/goanywhere-agent`, `fortrallc/goanywhere-gateway`), but the admin console and REST API need a trial or paid license key to do anything — Fortra doesn't publish a license-free mode — so the simulator is still the only zero-license way to smoke-test the full surface.

## Components used

| Component | What it does |
|---|---|
| `goanywhere_integration` | Each declared GoAnywhere Project becomes a daily-partitioned Dagster asset with a retry policy. Ships 3 op-backed jobs (rerun / dashboard summary / reconcile), 2 sensors (external-execution monitor + inbound trigger from GoAnywhere via Dagster GraphQL), and an hourly reconciliation schedule. |

## What this demonstrates

- **Dagster wraps GoAnywhere Projects.** Materializing an asset runs the project via `POST .../gacmd/v1/projects` with the partition key as a `SCHEDULED_DATE` variable, polls to terminal state (Queued → Active → Successful).
- **GoAnywhere can trigger Dagster.** The `goanywhere_inbound_trigger` sensor confirms the wire-up when a GoAnywhere Project post-step (or an independent file-arrival Trigger) hits Dagster's GraphQL `launchRun` mutation.
- **Operational ops are Dagster jobs.** Rerun / dashboard-summary / reconcile are runnable from the Dagster UI.
- **Reconciliation as a scheduled job.** Every hour, compare GoAnywhere's job state to Dagster's materialization ledger and report drift.

## Asset + job + sensor + schedule graph

```
Assets   ── goanywhere_partner_edi_inbound   [daily partitioned, kinds: python + goanywhere]
         ── goanywhere_regulatory_upload     [daily partitioned, kinds: python + goanywhere]
         ── goanywhere_partner_edi_landed    [source, kinds: goanywhere + file]

Jobs     ── goanywhere_rerun_project         (run config: {project_name})
         ── goanywhere_dashboard             (service status / sessions / transfer summary)
         ── goanywhere_reconciliation        (compare GoAnywhere state vs Dagster; report drift)

Sensors  ── goanywhere_external_execution_monitor  (poll every 60s)
         ── goanywhere_inbound_trigger             (GoAnywhere -> Dagster GraphQL)

Schedule ── goanywhere_reconciliation_schedule     (cron: 0 * * * *)
```

## Live output — one partition materialization in demo mode

Actual run logs (setup script materializes partition `2024-06-01` end-to-end):

```
goanywhere_partner_edi_inbound - [AUTH]   Authorization: Basic c3ZjX2RhZ3N0... (HTTP Basic)
goanywhere_partner_edi_inbound - [RUN]    POST https://goanywhere.internal/goanywhere/rest/gacmd/v1/projects -> jobId=373254
goanywhere_partner_edi_inbound -   Body: {
  "runParameters": {
    "project": "PARTNER_EDI_INBOUND",
    "domain": null,
    "jobName": "PARTNER_EDI_INBOUND",
    "jobQueue": "SFTP_INBOUND",
    "mode": "NORMAL",
    "priority": 5,
    "variables": {
      "SCHEDULED_DATE": "2024-06-01"
    }
  }
}
goanywhere_partner_edi_inbound - [POLL]   GET https://goanywhere.internal/goanywhere/rest/gacmd/v1/jobs/373254?jobStatusFormat=JSON -> status=Q
goanywhere_partner_edi_inbound - [POLL]   GET https://goanywhere.internal/goanywhere/rest/gacmd/v1/jobs/373254?jobStatusFormat=JSON -> status=A
goanywhere_partner_edi_inbound - [POLL]   GET https://goanywhere.internal/goanywhere/rest/gacmd/v1/jobs/373254?jobStatusFormat=JSON -> status=A
goanywhere_partner_edi_inbound - [POLL]   GET https://goanywhere.internal/goanywhere/rest/gacmd/v1/jobs/373254?jobStatusFormat=JSON -> status=S
goanywhere_partner_edi_inbound - [DONE]   PARTNER_EDI_INBOUND -> Successful (jobId: 373254)
```

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_goanywhere_integration_demo.sh \
  -o setup_goanywhere_integration_demo.sh
bash setup_goanywhere_integration_demo.sh
```

Requirements: `uv`. $0, ~15s first run, zero external dependencies in demo mode.

```bash
cd goanywhere-demo
uv run dg dev
```

Then in the UI: click a partition (`2024-06-01` or later) → **Materialize**. Watch the full GACMD REST API trace stream into the run logs — every POLL shows the exact REST call the production executor makes.

The setup script also materializes partition `2024-06-01` at the end so you see a green run before you even open the UI.

Cleanup when done: `rm -rf goanywhere-demo`

## REST API endpoints exercised (GACMD)

```
POST .../gacmd/v1/projects                       — run a GoAnywhere Project (runParameters body) -> jobId
GET  .../gacmd/v1/jobs/{jobId}?jobStatusFormat=JSON  — poll job status
GET  .../status                                  — service status (dashboard-style, path representative)
GET  .../sessions/active                         — active transfer sessions (path representative)
GET  .../transfers/summary                       — transfer summary (path representative)
```

Status codes: `Q` (queued), `A` (active), `P` (paused), `S` (successful), `F` (failed), `C` (cancelled). Terminal: `S | F | C`.

Fortra doesn't host a single public GACMD REST reference page the way SMA does for OpCon — this shape is confirmed from Fortra's own product/automation pages and forum posts, not one authoritative spec. Verify exact field names against your instance's built-in REST documentation (Admin > Documentation) before going to production.

## The hybrid deployment story

GoAnywhere is designed for the shape where you're **not** migrating off it — GoAnywhere keeps owning the file-transfer-centric batch that only an MFT platform does well (SFTP/AS2/PGP, partner EDI, compliance-grade encryption and audit trails), Dagster owns the cloud / analytics / AI pipeline downstream of the landed files, both share a single Dagster UI with correct lineage.

If you need generic workload scheduling rather than managed file transfer specifically, the scheduler-family siblings below (JAMS, Control-M, OpCon) fit better. This component keeps GoAnywhere in the loop as the file-transfer system of record.

## See also

- Sister components (similar asset / op / sensor / schedule shape):
  - [`jams_integration`](jams_integration.md) — Fortra JAMS Scheduler
  - [`controlm_integration`](controlm_integration.md) — BMC Control-M
  - [`opcon_integration`](opcon_integration.md) — SMA/Continuous OpCon
- Component reference: <https://dagster-component-ui.vercel.app/c/goanywhere_integration>
- GoAnywhere Docker images: <https://hub.docker.com/u/fortrallc>
- Walkthrough index: [examples/README.md](README.md)
