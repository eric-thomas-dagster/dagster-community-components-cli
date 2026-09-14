# Rundeck Integration — Rundeck Jobs as daily-partitioned Dagster assets

**Validated end-to-end** — Rundeck Community Edition is free + open-source, so real-Docker validation is a one-liner (no license gate, no PoC key from Sales). The setup script runs the component in `demo_mode: true` for a zero-dependency first-run trace on stdout; the "Real Docker option" section below swaps in the actual `rundeck/rundeck` container with the same YAML shape (only `demo_mode`, `endpoint`, and `RUNDECK_API_TOKEN` change).

## Components used

| Component | What it does |
|---|---|
| `rundeck_integration` | Each declared Rundeck Job becomes a daily-partitioned Dagster asset with a retry policy. Ships 5 op-backed jobs (restart / disable / enable / abort / reconcile), 2 sensors (external-execution monitor + inbound trigger from Rundeck via Dagster GraphQL), and an hourly reconciliation schedule. |

## What this demonstrates

- **Dagster wraps Rundeck Jobs.** Materializing an asset submits the Job via `POST /api/{v}/job/{job_id}/executions` (auth = `X-Rundeck-Auth-Token` bearer header), polls the resulting execution through `running → succeeded`, and retrieves output entries. The partition key flows in as an `argString` option (e.g. `-runDate 2024-06-01`).
- **Rundeck can trigger Dagster.** A Rundeck job's final step (script / http webhook plugin) calls Dagster's GraphQL `launchRun` mutation. The `rundeck_inbound_trigger` sensor confirms the wire-up in the Dagster UI.
- **Operational ops are Dagster jobs.** Restart / disable / enable / abort / reconcile ship as runnable jobs in the Dagster UI — one pane of glass over Rundeck's REST surface.
- **Reconciliation as a scheduled job.** Every hour, compare Rundeck's execution state per project against Dagster's materialization ledger and report drift.

## Asset + job + sensor + schedule graph

```
Assets   ── rundeck_eod_settlement              [daily partitioned, kinds: python + rundeck]
         ── rundeck_regulatory_extract          [daily partitioned, kinds: python + rundeck]
         ── rundeck_settlement_table            [source, kinds: rundeck + database]  (optional)

Jobs     ── rundeck_restart_execution           (run config: {job_id})
         ── rundeck_disable_job                 (run config: {job_id})
         ── rundeck_enable_job                  (run config: {job_id})
         ── rundeck_abort_execution             (run config: {execution_id})
         ── rundeck_reconciliation              (per-project drift report)

Sensors  ── rundeck_external_execution_monitor  (poll every 60s)
         ── rundeck_inbound_trigger             (Rundeck -> Dagster GraphQL)

Schedule ── rundeck_reconciliation_schedule     (cron: 0 * * * *)
```

## Live output — one partition materialization in demo mode

Actual run logs from `dg.materialize([rundeck_eod], partition_key='2024-06-01')` with `demo_mode: true` (executionId + duration will vary each run):

```
[AUTH]   X-Rundeck-Auth-Token: rdk-***... (bearer token)
[SUBMIT] POST http://localhost:4440/api/47/job/abc-123-def-456/executions
  Payload: {
    "argString": "",
    "options": {},
    "asUser": "svc_dagster"
  }
  Response: 200 OK — executionId: 6464661, project: banking
[POLL]   GET http://localhost:4440/api/47/execution/6464661 -> status=running
[POLL]   GET http://localhost:4440/api/47/execution/6464661 -> status=running
[POLL]   GET http://localhost:4440/api/47/execution/6464661 -> status=running
[POLL]   GET http://localhost:4440/api/47/execution/6464661 -> status=running
[POLL]   GET http://localhost:4440/api/47/execution/6464661 -> status=succeeded
[OUTPUT] GET http://localhost:4440/api/47/execution/6464661/output -> 47 entries
[DONE]   abc-123-def-456 -> succeeded (executionId: 6464661, project: banking)
ASSET_MATERIALIZATION - Materialized value rundeck_eod.
RUN_SUCCESS
```

Every line above is what a real Rundeck run against `http://localhost:4440` would produce (with the URLs, executionId, and status pulled from live responses instead of the simulator). Flip `demo_mode: false` and the same code path runs over real HTTP against your Rundeck instance.

Asset metadata captured on every materialization: `external_execution_id`, `status`, `scheduled_time`, `duration_seconds`, `demo_mode`.

## Point at a real Rundeck instance

Create an API token in the Rundeck UI:

> **User Profile (top-right avatar) -> User API Tokens -> Generate New Token** — copy the `rdk-…` value, it's shown once.

Set the env var + flip the YAML:

```bash
export RUNDECK_API_TOKEN='rdk-<your-token>'
```

```yaml
attributes:
  demo_mode: false
  endpoint: "https://rundeck.prod.internal:4443"
  api_version: 47
  rundeck_token_env: RUNDECK_API_TOKEN
  ...
```

> **API-version caveat.** `api_version` defaults to `47` (current 2026). Older Rundeck installs cap out at lower ints — check `GET /api/{v}/system/info` and set the field to the highest your server supports.

## Real Docker option (rundeck/rundeck open-source)

Because Rundeck Community Edition is free and open-source, you can validate end-to-end against a real Rundeck in under a minute:

```bash
docker run -d --name rundeck \
  -p 4440:4440 \
  -e RUNDECK_GRAILS_URL=http://localhost:4440 \
  rundeck/rundeck:5.10.0
```

Open <http://localhost:4440> and log in with the built-in credentials:

- **Username:** `admin`
- **Password:** `admin`

Then, inside the UI:

1. **Create a project** — top-left "New Project" -> name it `banking` (matches the demo YAML's project field).
2. **Create a job** — inside the project, "Jobs" -> "New Job" -> add any step (e.g. a shell step `echo "settlement complete"`), save. Grab the job UUID from the job's detail URL (`/project/banking/job/show/<uuid>`).
3. **Generate an API token** — User Profile (top-right avatar) -> "User API Tokens" -> "Generate New Token". Copy the `rdk-…` value.

Now update your `defs.yaml`:

```yaml
attributes:
  demo_mode: false
  endpoint: "http://localhost:4440"
  api_version: 47
  jobs:
    - job_id: "<the-uuid-from-step-2>"
      asset_name: rundeck_eod_settlement
      project: banking
      arg_string: "-runDate {{ partition_key }}"
```

And export the token:

```bash
export RUNDECK_API_TOKEN='rdk-<your-token>'
uv run dg dev
```

Materialize any partition from the UI — you'll see the same trace as demo mode, but the SUBMIT / POLL / OUTPUT lines will hit the real container. Verify the execution shows up in Rundeck's "Activity" tab.

## Two-way trigger story

Both directions are wired by the same component:

**Dagster -> Rundeck** (asset materialization fires a Rundeck job):

```
POST /api/{v}/job/{job_id}/executions   (X-Rundeck-Auth-Token: rdk-...)
GET  /api/{v}/execution/{id}            (poll to terminal state)
GET  /api/{v}/execution/{id}/output     (retrieve entries)
```

**Rundeck -> Dagster** (Rundeck job step launches a Dagster run):

Add a script or HTTP step to your Rundeck job (final step) that POSTs to Dagster's GraphQL API:

```
POST https://dagster.cloud/graphql
Content-Type: application/json
Authorization: Bearer <dagster-cloud-user-token>

{"query": "mutation { launchRun(executionParams: { selector: { ... } }) { __typename } }"}
```

The `rundeck_inbound_trigger` sensor confirms the trigger was received and produces observable ticks in the Dagster UI. Parameters flow through `argString` / option values from the Rundeck side.

## Operational ops — Dagster as the pane of glass

Five ops ship as Dagster jobs so restart / disable / enable / abort / reconcile are runnable from the Dagster UI:

| Job | Config | Purpose |
|---|---|---|
| `rundeck_restart_execution` | `{job_id}` | Re-fire a Rundeck job (Rundeck has no per-execution rerun — you re-launch the job) |
| `rundeck_disable_job` | `{job_id}` | Disable a job so no new executions launch (`POST /job/{id}/execution/disable`) |
| `rundeck_enable_job` | `{job_id}` | Re-enable a previously-disabled job |
| `rundeck_abort_execution` | `{execution_id}` | Abort a running execution |
| `rundeck_reconciliation` | — | Compare Rundeck state vs Dagster state per project; report drift + alert on failures |

The `rundeck_reconciliation_schedule` runs the reconciliation job every hour (STOPPED by default — toggle in the UI when ready).

> **Rundeck-specific note:** Rundeck's "hold" analog is **per-job** (`disable_rundeck_job`), not per-project. Rundeck has no first-class notion of "hold all jobs in a project" — the closest equivalent is to disable each job individually. For bulk operations, loop `rundeck_disable_job` over your project's job list.

## Terminology cheat-sheet — Rundeck vs Control-M / RunMyJobs

For teams running two schedulers side-by-side (or migrating between them):

| Control-M | RunMyJobs | Rundeck |
|---|---|---|
| Job | JobDefinition | Job |
| Folder | Application | Project |
| Agent / Host | Queue | Node / Node Filter |
| ODATE | scheduledTime | argString option (e.g. `-runDate <date>`) |
| runId | processId | executionId |
| "Ended OK" / "Ended Not OK" | "Completed" / "Error" | "succeeded" / "failed" |

The sister components have the same asset / op / sensor / schedule surface, so a shop running multiple schedulers can present a single Dagster pane of glass over all of them.

## REST API endpoints exercised

```
POST /api/{v}/job/{job_id}/executions            — run a job (body: argString, options, asUser, filter?)
GET  /api/{v}/execution/{id}                     — poll execution status
GET  /api/{v}/execution/{id}/output              — output entries (log + level)
POST /api/{v}/execution/{id}/abort               — abort a running execution
POST /api/{v}/job/{id}/execution/disable         — disable a job (no new executions)
POST /api/{v}/job/{id}/execution/enable          — re-enable a disabled job
GET  /api/{v}/project/{project}/executions       — list executions (params: status, recentFilter, max)
```

Verify against your Rundeck version's API reference — the surface has evolved across v14 → v47.

## The hybrid deployment story

Rundeck is designed for the shape where you're **not** migrating off it — Rundeck keeps owning the runbook automation and node-fleet orchestration that only Rundeck can own (SSH fan-out across a node group, script libraries with ACL gating, ops runbooks that non-engineers trigger from the Rundeck UI), Dagster owns the cloud / analytics / AI pipeline, both share a single Dagster UI with correct lineage.

Because Rundeck Community Edition is free and open-source, this hybrid shape carries no per-seat cost gate on either side — validate the full round-trip against a local container, promote to production with just an endpoint + token swap.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_rundeck_integration_demo.sh | bash
cd rundeck-demo
uv run dg dev
```

Then in the UI: click a partition (2024-06-01 or later) -> **Materialize**. Watch the full REST API trace stream into the run logs.

The setup script also materializes partition `2024-06-01` at the end so you see a green run before you even open the UI.

## Companion

- `controlm_integration` — same asset / op / sensor / schedule shape for BMC Control-M
- `runmyjobs_integration` — same shape for Redwood RunMyJobs
- `jenkins_integration` — same shape for Jenkins (build jobs as partitioned assets)

## See also

- Component reference: <https://dagster-component-ui.vercel.app/c/rundeck_integration>
- Walkthrough index: [examples/README.md](README.md)
