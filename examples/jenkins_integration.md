# Jenkins Integration — Jenkins jobs as daily-partitioned Dagster assets

**Validated end-to-end** — the setup script scaffolds a Dagster project, installs the `jenkins_integration` component in `demo_mode: true`, and materializes one daily partition. The simulator walks the FULL Jenkins REST lifecycle on stdout (`AUTH → CRUMB → TRIGGER → QUEUE → POLL → CONSOLE → DONE`) so the whole Dagster surface — assets, ops, sensors, schedule — loads and materializes with zero external dependencies. Flip `demo_mode: false` and export `JENKINS_USER` / `JENKINS_API_TOKEN` to hit a real Jenkins controller (or spin up `jenkins/jenkins:lts` in Docker — instructions below).

## Components used

| Component | What it does |
|---|---|
| `jenkins_integration` | Each declared Jenkins job becomes a daily-partitioned Dagster asset with a retry policy. Ships 5 op-backed jobs (rebuild / disable / enable / stop / reconcile), 2 sensors (external-execution monitor + inbound trigger from Jenkins via Dagster GraphQL), and an hourly reconciliation schedule. |

## What this demonstrates

- **Dagster wraps Jenkins jobs.** Materializing an asset acquires a CSRF crumb, triggers the job via `POST /job/<name>/build` (or `/buildWithParameters` if params are declared) with HTTP Basic Auth + API token, tracks the queue item until a build number lands, polls the build to terminal result, and pulls consoleText.
- **Jenkins can trigger Dagster.** The `jenkins_inbound_trigger` sensor confirms the wire-up when a Jenkins post-build step (curl / `httpRequest` Groovy) hits Dagster's GraphQL `launchRun` mutation.
- **Operational ops are Dagster jobs.** Rebuild / disable / enable / stop / reconcile are runnable from the Dagster UI (or via GraphQL).
- **Reconciliation as a scheduled job.** Every hour, compare Jenkins' state (blue / red / yellow / disabled) to Dagster's materialization ledger and report drift.

## Asset + job + sensor + schedule graph

```
Assets   ── jenkins_eod_settlement            [daily partitioned, kinds: python + jenkins]
         ── jenkins_regulatory_extract        [daily partitioned, kinds: python + jenkins]
         ── jenkins_settlement_table          [source, kinds: jenkins + database]  (optional)

Jobs     ── jenkins_rebuild_job               (run config: {job_name})
         ── jenkins_disable_job               (run config: {job_name})       — analog to Control-M hold
         ── jenkins_enable_job                (run config: {job_name})       — release from disabled
         ── jenkins_stop_build                (run config: {job_name, build_number})
         ── jenkins_reconciliation            (compare Jenkins colors vs Dagster; report drift)

Sensors  ── jenkins_external_execution_monitor  (poll every 60s)
         ── jenkins_inbound_trigger             (Jenkins post-build -> Dagster GraphQL)

Schedule ── jenkins_reconciliation_schedule     (cron: 0 * * * *)
```

## Live output — one partition materialization in demo mode

Actual run logs (setup script materializes partition `2024-06-01` end-to-end):

```
jenkins_eod_settlement - [AUTH]    Authorization: Basic c3ZjX2RhZ3N0... (HTTP Basic + API token)
jenkins_eod_settlement - [CRUMB]   GET http://localhost:8080/crumbIssuer/api/json
jenkins_eod_settlement -   Response: {crumb: 'ab12cd34ef56', crumbRequestField: 'Jenkins-Crumb'}
jenkins_eod_settlement - [TRIGGER] POST http://localhost:8080/job/banking/job/eod-batch-settlement/buildWithParameters
jenkins_eod_settlement -   Params: {"PARTITION_KEY": "2024-06-01", "SETTLEMENT_DATE": "2024-06-01"}
jenkins_eod_settlement -   Response: 201 Created
jenkins_eod_settlement -   Location: http://localhost:8080/queue/item/178FAB/
jenkins_eod_settlement - [QUEUE]   GET http://localhost:8080/queue/item/178FAB/api/json -> waiting
jenkins_eod_settlement - [QUEUE]   GET http://localhost:8080/queue/item/178FAB/api/json -> executable.number=6744
jenkins_eod_settlement - [POLL]    GET http://localhost:8080/job/banking/job/eod-batch-settlement/6744/api/json -> building=true, result=None
jenkins_eod_settlement - [POLL]    GET http://localhost:8080/job/banking/job/eod-batch-settlement/6744/api/json -> building=true, result=None
jenkins_eod_settlement - [POLL]    GET http://localhost:8080/job/banking/job/eod-batch-settlement/6744/api/json -> building=true, result=None
jenkins_eod_settlement - [POLL]    GET http://localhost:8080/job/banking/job/eod-batch-settlement/6744/api/json -> building=false, result=SUCCESS
jenkins_eod_settlement - [CONSOLE] GET http://localhost:8080/job/banking/job/eod-batch-settlement/6744/consoleText -> 512 lines
jenkins_eod_settlement - [DONE]    banking/eod-batch-settlement -> SUCCESS (buildNumber: 6744, partition: 2024-06-01)
ASSET_MATERIALIZATION - Materialized value jenkins_eod_settlement.
RUN_SUCCESS
```

Every `[POLL]` line in production mode is a real HTTP GET against Jenkins with the CSRF crumb header + Basic Auth. The simulator matches the shape exactly so you see the same trace whether you're in demo mode or pointed at a real controller.

Asset metadata captured on every materialization: `build_number`, `result`, `partition`, `duration_seconds`, `demo_mode`.

## The transition-phase story

Most Jenkins-migration engagements aren't "kill Jenkins overnight" — they're multi-quarter. `jenkins_integration` is designed for the **transition phase**: Dagster owns the new pipelines (and increasingly, older pipelines migrated one at a time), Jenkins keeps owning what it already runs (CI builds, legacy freestyle jobs, plugin-glued workflows), and both are visible from the same Dagster UI with correct lineage.

As you migrate individual jobs off Jenkins, you delete the corresponding YAML entry and the asset simply disappears from the graph. The rest of the pipeline (upstream ingestion assets, downstream analytics assets, sensors, ops) doesn't budge. When Jenkins is finally empty, the whole component YAML file goes away with it.

## Point at a real Jenkins

Jenkins credentials use an **API token**, not the user's password. Generate one from `http://<jenkins>/user/<username>/configure` -> "API Token" section -> "Add new Token".

```bash
export JENKINS_USER=svc-dagster
export JENKINS_API_TOKEN=11a2b3c4d5e6f7g8h9i0j1k2l3m4n5o6p7
```

Then in `defs.yaml`:

```yaml
attributes:
  demo_mode: false
  endpoint: "http://jenkins.prod.internal:8080"
  ...
```

Everything else stays exactly as the demo — same job list, same partition shape, same ops. The component takes care of the CSRF crumb dance (required for POST since Jenkins 2.222).

## Real Docker option (jenkins/jenkins:lts)

Jenkins ships a first-party Docker image with no license restrictions — the fastest way to try the real-API path end-to-end without touching production:

```bash
# 1. Spin up Jenkins
docker run -d --name jenkins-demo -p 8080:8080 -p 50000:50000 \
  -v jenkins_home:/var/jenkins_home \
  jenkins/jenkins:lts

# 2. Wait ~30s for boot, then grab the initial admin password
docker exec jenkins-demo cat /var/jenkins_home/secrets/initialAdminPassword
```

Open <http://localhost:8080>, paste the initial admin password, complete the setup wizard (install suggested plugins, create an admin user). Then:

1. **Generate an API token** — click your user avatar (top-right) -> Configure -> "API Token" -> "Add new Token" -> "Generate". Copy the token.
2. **Create a freestyle job** — New Item -> name it `eod-batch-settlement` (or nest it under a folder named `banking`) -> Freestyle project.
3. **Add a build parameter** — check "This project is parameterized" -> Add String Parameter -> name `SETTLEMENT_DATE`, default `2024-01-01`.
4. **Add an "Execute shell" build step** — `echo "Settlement for $SETTLEMENT_DATE"; sleep 5`.
5. **Save**, then point the component at Jenkins:

```bash
export JENKINS_USER=<the admin username you just created>
export JENKINS_API_TOKEN=<the API token you just copied>
```

Set `demo_mode: false` + `endpoint: "http://localhost:8080"` in `defs.yaml` and materialize a partition — you should see the same `AUTH → CRUMB → TRIGGER → QUEUE → POLL → CONSOLE → DONE` trace, but each line will be a real HTTP call against your Docker Jenkins.

Cleanup: `docker rm -f jenkins-demo && docker volume rm jenkins_home`.

## Two-way trigger story

**Dagster -> Jenkins** (assets):
Each declared job becomes a daily-partitioned Dagster asset. Materialization sequence:

1. `GET /crumbIssuer/api/json` — acquire CSRF crumb (required for POST since 2.222; the component tolerates 404 for older Jenkins that has CSRF disabled)
2. `POST /job/<folder>/job/<name>/build[WithParameters]` — trigger, with the crumb header + HTTP Basic Auth (user + API token). Params: `{PARTITION_KEY, ...templated with {partition_key}}`
3. `GET /queue/item/<id>/api/json` — poll queue item until `executable.number` is assigned
4. `GET /job/.../<build_number>/api/json` — poll build until `building=false` + terminal result
5. `GET /job/.../<build_number>/consoleText` — fetch console output

Retries: `RetryPolicy(max_retries=2, delay=60s)` on the Dagster asset — HTTP timeouts, queue-never-launches, and non-SUCCESS builds all trigger a Dagster retry.

**Jenkins -> Dagster** (inbound trigger sensor):
Production wire-up: your Jenkins post-build step is a `curl` (or `httpRequest` Groovy step) calling Dagster's GraphQL `launchRun` mutation. Example:

```groovy
// Jenkinsfile post-build step
httpRequest(
  httpMode: 'POST',
  url: 'https://your.dagster.cloud/graphql',
  customHeaders: [[name: 'Dagster-Cloud-Api-Token', value: "${DAGSTER_TOKEN}"]],
  requestBody: '{ "query": "mutation { launchRun(executionParams: { ... }) }" }',
)
```

The `jenkins_inbound_trigger` sensor (STOPPED by default — toggle in the UI when you wire it up) produces observable ticks confirming the inbound trigger was received.

## Operational ops — Dagster as pane of glass

Five ops ship as Dagster jobs so you can rebuild / disable / enable / stop / reconcile Jenkins jobs directly from the Dagster UI without leaving the pane of glass:

| Job | Config | Purpose |
|---|---|---|
| `jenkins_rebuild_job` | `{job_name}` | Rebuild a job (`POST /job/<name>/build`) |
| `jenkins_disable_job` | `{job_name}` | Disable a job — analog to Control-M `hold` |
| `jenkins_enable_job` | `{job_name}` | Re-enable a previously disabled job |
| `jenkins_stop_build` | `{job_name, build_number}` | Stop a running build (`POST /job/<name>/<b>/stop`) |
| `jenkins_reconciliation` | — | Compare Jenkins color state (blue/red/yellow/disabled) vs Dagster's materialization ledger; report drift + FAILURE alerts |

The `jenkins_reconciliation_schedule` runs the reconciliation job every hour (STOPPED by default — toggle in the UI when you're ready).

## Terminology cheat-sheet — Jenkins vs Control-M / RunMyJobs

For teams running Jenkins alongside another scheduler (or migrating from one to the other), the concept map:

| Control-M | RunMyJobs | Jenkins |
|---|---|---|
| Job | JobDefinition | Job (freestyle or pipeline) |
| Folder | Application | Folder (Folders plugin) |
| Agent / Host | Queue | Node (agent/executor with a label) |
| ODATE | scheduledTime | Build parameter (typically) |
| runId | processId | Build number |
| "Ended OK" / "Ended Not OK" | "Completed" / "Error" | "SUCCESS" / "FAILURE" / "ABORTED" |

The [`controlm_integration`](controlm_integration.md), [`runmyjobs_integration`](runmyjobs_integration.md), and [`rundeck_integration`](rundeck_integration.md) sister components share the same asset / op / sensor / schedule shape — a shop running Jenkins alongside another scheduler during migration can present a single Dagster pane of glass over all of them.

## REST API endpoints exercised

```
GET  /crumbIssuer/api/json                        — acquire CSRF crumb (required for POST since 2.222)
POST /job/<folder>/job/<name>/build               — trigger unparameterized job
POST /job/<folder>/job/<name>/buildWithParameters — trigger with build parameters
GET  /queue/item/<id>/api/json                    — poll queue item until executable.number lands
GET  /job/<folder>/job/<name>/<b>/api/json        — poll build until building=false + terminal result
GET  /job/<folder>/job/<name>/<b>/consoleText     — fetch console output
POST /job/<folder>/job/<name>/disable             — disable (hold) a job
POST /job/<folder>/job/<name>/enable              — re-enable a disabled job
POST /job/<folder>/job/<name>/<b>/stop            — stop a running build
GET  /api/json?tree=jobs[name,color,lastBuild[…]] — reconciliation snapshot
```

All requests carry `Authorization: Basic <base64(user:api_token)>` + the CSRF crumb header (`Jenkins-Crumb` by default) on POSTs.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_jenkins_integration_demo.sh | bash
cd jenkins-demo
uv run dg dev
```

Then in the UI: click a partition (2024-06-01 or later) on `jenkins_eod_settlement` -> **Materialize**. Watch the full simulated REST trace stream into the run logs. Flip `demo_mode: false` + export `JENKINS_USER` / `JENKINS_API_TOKEN` to hit a real Jenkins (or the Docker option above).

The setup script also materializes partition `2024-06-01` at the end so you see a green run before you even open the UI.

## Companion

Sister components with the same asset / op / sensor / schedule surface — pick the one that matches the scheduler you're wrapping (or install several to cover a multi-scheduler shop mid-migration):

- [`controlm_integration`](controlm_integration.md) — BMC Control-M
- [`runmyjobs_integration`](runmyjobs_integration.md) — Redwood RunMyJobs
- [`rundeck_integration`](rundeck_integration.md) — Rundeck

## See also

- Component reference: <https://dagster-component-ui.vercel.app/c/jenkins_integration>
- Walkthrough index: [examples/README.md](README.md)
