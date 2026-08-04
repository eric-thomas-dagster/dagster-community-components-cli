# External Scheduler demo
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

How to keep an existing master scheduler — Control-M, Autosys, CA WA ESP, Tidal, IBM TWS, JAMS, Stonebranch, Redwood, Airflow, cron,
Jenkins, anything — in charge of *when* a Dagster job runs, without writing
a Dagster integration component for it.

The scheduler stays the source of truth for cadence, retries, and SLA
alerting. Dagster owns *how* the work runs (lineage, asset graph, execution).
The bridge between them is a shell script that calls Dagster's GraphQL API.

```
   external scheduler  ──────┐
   (Control-M, ESP, Autosys, │ shells out
   cron, Jenkins, ...)       ▼
                          bin/kick_off_run.sh
                              │ POST /graphql launchRun
                              │   jobName=daily_revenue_refresh
                              │   tags={dagster/partition: <ODATE>}
                              ▼
                          ┌──────────────────────────────────────────┐
                          │  asset_job   (daily_revenue_refresh)     │  ← the named, scheduler-stable target
                          │    │                                      │
                          │    └─→ csv → summarize → csv (per partition)
                          └──────────────────────────────────────────┘
```

## Why GraphQL, not the Dagster CLI?

Real schedulers run on agents that **don't have `uv` or `dg` installed**.
Control-M agents typically run on AIX, zLinux, or hardened Windows boxes
where adding the Dagster CLI is governance-heavy. They all have `curl`.

So the integration is one shell script: `curl` POSTs a `launchRun` mutation
to Dagster's GraphQL endpoint, polls for completion, and exits with a status
code the scheduler reads natively.

## Why no Dagster component for "external scheduler"?

A "Control-M component" or "Autosys component" implies bidirectional
integration, which fights the pattern. Customers want **one-way**: the
scheduler stays in charge, Dagster gets called. That's a script, not a
component.

## What gets scaffolded

The setup script writes:

1. A normal daily-partitioned Dagster project (3 components: csv → summarize → csv)
2. `bin/kick_off_run.sh` — the GraphQL invocation script (production shape)
3. `bin/kick_off_run_via_cli.sh` — a CLI shortcut for **local testing only**

## Components used

The four community components below compose into the full pattern. The
star is **`asset_job`** — without it, the scheduler has no stable name to
target.

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `file_ingestion` | ingestion | Read partitioned source CSV |
| 2 | `summarize` | transformation | Per-partition group-by aggregate |
| 3 | `dataframe_to_csv` | sink | Write `daily_revenue_{partition_key}.csv` |
| 4 | **`asset_job`** | infrastructure | **Bundles the 3 assets above into the named job `daily_revenue_refresh` that the scheduler launches.** Without this, you'd target `__ASSET_JOB` (auto-generated, materializes *everything*), so the scheduler would silently start running newly-added assets it never authorized. The `asset_job` keeps the scheduler-side contract stable: *"run job=daily_revenue_refresh, partition=$ODATE"*. |

The three asset components describe *what* runs; `asset_job` carves out the
exact slice the external scheduler is allowed to invoke. The Dagster project
itself doesn't know or care about the scheduler — that's the design.

### Is `asset_job` required?

**No** — the scheduler has three options for what to target. `asset_job`
is the recommended one once you have more than one or two assets, but the
others work:

| Approach | scheduler-side payload | When it's OK |
|---|---|---|
| **Named `asset_job` (this demo)** | `jobName=daily_revenue_refresh` | Recommended. Stable contract; safe as the project grows. |
| **`__ASSET_JOB` + asset selection** | `jobName=__ASSET_JOB` + `assetSelection: ["daily_revenue_report"]` in run config | Works. But `__ASSET_JOB` is an implementation detail, and the asset list now lives on both sides — your scheduler config and the Dagster project. Two sources of truth = drift. |
| **Single asset, no job wrapper** | `dg launch --assets daily_revenue_report --partition $ODATE` (CLI) | Fine when there's exactly one asset to materialize. Once it grows, you're back to one of the above. |

The named `asset_job` is the version you'd ship to customers. It's a stable
GraphQL contract that doesn't change as you add unrelated assets — the
scheduler stays out of the asset graph entirely.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_external_scheduler_demo.sh | bash
cd external-scheduler-demo
```

### Local test (production-shape: GraphQL)

The webserver has to be running for GraphQL to be reachable. In one terminal:

```bash
uv run dg dev   # starts at http://localhost:3000
```

In a second terminal — the scheduler simulation:

```bash
DAGSTER_GRAPHQL_URL=http://localhost:3000/graphql \
REPO_LOCATION=external_scheduler_demo \
bin/kick_off_run.sh 2026-05-01
```

Auth: **OSS Dagster has no auth on its GraphQL endpoint**, so this works as
shown above. For **Dagster+**, set `DAGSTER_PLUS_USER_TOKEN` and point
`DAGSTER_GRAPHQL_URL` at `https://<org>.dagster.cloud/<deployment>/graphql`.
The script reads the token and adds the `Dagster-Cloud-Api-Token` header.

### Local test (shortcut: CLI)

```bash
bin/kick_off_run_via_cli.sh 2026-05-01
```

Wraps `dg launch --partition <key>`. Skips the webserver entirely. **Only
useful for local testing** because it requires `uv` + the Dagster project
checked out — that's not how Control-M agents look.

## Wiring to real schedulers

```
Control-M:    job step calls   bin/kick_off_run.sh %%$ODATE
Autosys:      command:         bin/kick_off_run.sh $$AUTODATE
CA WA ESP:    INVOKE command   bin/kick_off_run.sh %ESP.APPL.BIZ_DATE%
Tidal:        command:         bin/kick_off_run.sh ${TID_BUS_DATE}
IBM TWS:      script step:     bin/kick_off_run.sh ^DATE^
JAMS:         execution method bin/kick_off_run.sh {{$Date.Today}}
Stonebranch:  Universal Task   bin/kick_off_run.sh ${BUSINESS_DATE}
Redwood RMS:  Process script   bin/kick_off_run.sh #{ScheduleDate}
Airflow:      BashOperator:    bin/kick_off_run.sh {{ ds }}
cron:         crontab line:    0 2 * * *  cd /opt/proj && bin/kick_off_run.sh
Jenkins:      shell step:      sh "bin/kick_off_run.sh ${BUILD_DATE}"
```

The pattern is identical across all of them: the master scheduler shells out to a small script that calls Dagster's GraphQL `launchRun` mutation, polls the run status until terminal, and returns 0 on success / non-zero otherwise. Whatever the scheduler's date-substitution variable is (Control-M's `%%$ODATE`, ESP's `%ESP.APPL.BIZ_DATE%`, Autosys's `$$AUTODATE`, Airflow's `{{ ds }}`), pass it as the script's first argument and the run inherits it via tag or run config.

## Observability — the reverse direction (per-scheduler)

The pattern above is **universal** for the kick-off direction (scheduler → Dagster). The reverse — Dagster knowing what the scheduler has scheduled / running / last-ran — is **per-scheduler** because every workload-automation product has its own API and concept model:

| Scheduler | API for observing jobs / runs | Component status |
|---|---|---|
| Control-M | REST API (`/automation-api/run/jobs`, etc.) | not in registry — would need a custom `control_m_observation_sensor` |
| CA WA ESP | iXp REST API + console screens | not in registry — would need `esp_observation_sensor` |
| Autosys | WAAE REST API / `autorep -J` shell command | not in registry — would need `autosys_observation_sensor` |
| IBM TWS | conman REST / Z/OS DDR | not in registry |
| JAMS | JAMS REST API + PowerShell module | not in registry |
| Stonebranch | UAC REST API | not in registry |
| Redwood RMS | RMS REST API | not in registry |
| Airflow | Airflow REST API (`/api/v1/dagRuns`, etc.) | `airflow_dag_observation_sensor` — already in registry |
| cron | crontab parsing + system logs | not in registry — could be done; few people ask |
| Jenkins | Jenkins REST API | partial — `external_assets` covers declaring Jenkins jobs as external assets |

**The pattern for each is the same shape — only the SDK differs:**

```python
# Pseudo-code shared by every scheduler-observation sensor:
@sensor(asset_selection=AssetSelection.assets("scheduler_job_X"))
def scheduler_obs_sensor(context):
    client = <vendor SDK>
    jobs = client.list_jobs(filter=...)
    for job in jobs:
        if job.last_run_id not in seen(context.cursor):
            yield AssetObservation(
                asset_key="scheduler_job_X",
                metadata={
                    "scheduler_run_id": job.last_run_id,
                    "scheduler_status": job.status,
                    "scheduler_start_time": job.start_time.isoformat(),
                    "scheduler_business_date": job.business_date,
                    "scheduler_duration_seconds": job.duration_seconds,
                },
            )
    return SensorResult(cursor=...)
```

It's `~80 lines of vendor SDK glue` per scheduler — small enough to be tractable, distinct enough across products that one generic component can't reasonably wrap them all.

**If you need scheduler observability today**, the practical options:

1. **Build a custom component** following the shape above. The vendor's REST API doc is the only required reading; we ship a [`_template_observation_sensor`](../../dagster-component-templates/observations/) shape pattern that you can copy.
2. **Use `http_external_asset`** as a quick wrapper: declare the scheduled job as an external asset with an HTTP URL pointing at the scheduler's status endpoint. Less rich than a dedicated sensor (no per-run metadata mapping) but works as a stopgap.
3. **Open an issue** in the registry repo — we'll prioritize building one for whichever scheduler family you're on.

A custom component per scheduler is the right shape. The kick-off direction is trivial precisely because it's just `curl` to Dagster's GraphQL; the observation direction is per-scheduler precisely because every scheduler models "what's scheduled" differently.

The script returns 0 only on `LaunchRunSuccess` *and* a `success` final run
status. That's how the scheduler knows whether to retry, alert, or chain
forward.

## Trigger + monitor — Dagster kicks OFF scheduler jobs (future direction)

The two patterns above cover most real-world cases — scheduler stays in charge, or scheduler runs and Dagster observes. There's a *third* shape: **Dagster wants to trigger a scheduler-owned job and wait for its result before continuing**. Think of a Dagster asset that depends on a Control-M batch job, or a Dagster pipeline that needs to kick off an Autosys box and block until completion.

This pattern looks like an integration component:

```yaml
type: dagster_component_templates.AutosysJobAsset    # hypothetical
attributes:
  asset_name: nightly_mainframe_extract
  job_name: NIGHTLY_BOX
  poll_interval_seconds: 30
  timeout_seconds: 3600
  on_failure: raise    # or 'warn', 'skip'
```

At materialize time it would:
1. Call the scheduler's REST API to kick off the named job (or job stream)
2. Poll the same API until the job reaches a terminal state
3. Emit metadata: `scheduler_run_id`, `start_time`, `duration_seconds`, `exit_code`, `log_url`
4. Return `MaterializeResult` (success) or raise (failure) so downstream assets gate correctly

This is the shape that lets Dagster compose with a scheduler-owned dependency — useful when the upstream lives in Control-M/Autosys but the downstream lineage and observability live in Dagster.

### Status across schedulers

| Scheduler | Trigger API | Status / poll API | Community-fair-game? |
|---|---|---|---|
| **Control-M** | REST `/automation-api/run/order/` | REST `/automation-api/run/jobs/<id>` | **No — commercial reserve.** Control-M trigger+monitor is built as a separate paid offering. Won't ship to the community registry. |
| **IBM TWS / z/OS workload automation** | conman + Z/OS DDR | conman / DDR | **No — commercial reserve.** Same rationale as Control-M (mainframe footprint, paid engagement model). |
| **Autosys (Workload Automation AE)** | WAAE REST `/aedb/api/v2/jobs/<name>/run` | WAAE REST `/aedb/api/v2/jobs/<name>` | Yes — community can ship `autosys_job_asset` |
| **CA WA ESP** | iXp REST `/v1/executejob` | iXp REST `/v1/jobinfo` | Yes — community `esp_job_asset` |
| **JAMS** | JAMS REST `/api/Jobs/Submit` | `/api/History/<id>` | Yes — community `jams_job_asset` |
| **Stonebranch UAC** | UAC REST `/resources/task/runtask` | `/resources/taskinstance/<id>` | Yes — community `stonebranch_task_asset` |
| **Redwood RMS** | RMS REST `/api/processes/submit` | `/api/processes/<id>` | Yes — community `redwood_process_asset` |
| **Tidal** | Tidal REST `/api/scheduler/jobs/submit` | `/api/scheduler/jobs/<id>` | Yes — community `tidal_job_asset` |
| **Airflow** | Airflow REST `/api/v1/dags/<id>/dagRuns` | `/api/v1/dags/<id>/dagRuns/<run_id>` | **Use the official `dagster-airlift`** package — it's a richer Airflow-Dagster integration than a custom component would be. |
| **Jenkins** | Jenkins REST `/job/<name>/buildWithParameters` | `/job/<name>/<build>/api/json` | Yes — community `jenkins_job_asset` |
| **cron** | n/a — cron has no API | n/a | Not applicable; cron is fire-and-forget |

### Pattern for the community-fair-game ones

The component shape is uniform across schedulers — just like the observation-sensor shape was. ~150 lines of vendor SDK glue per scheduler:

```python
# Pseudo-code shared by every scheduler-trigger asset:
@asset(deps=[...])
def scheduler_owned_step(context: AssetExecutionContext):
    client = <vendor SDK>
    run_id = client.submit(job_name, parameters=...)
    context.log.info(f"Submitted {job_name} → run_id={run_id}")

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        status = client.get_status(run_id)
        if status.state in TERMINAL:
            break
        time.sleep(poll_interval_seconds)
    else:
        client.cancel(run_id)
        raise TimeoutError(f"{job_name} did not finish in {timeout_seconds}s")

    if status.state != SUCCESS:
        raise RuntimeError(f"{job_name} failed: {status.message}")

    return MaterializeResult(metadata={
        "scheduler_run_id":      MetadataValue.text(run_id),
        "scheduler_start_time":  MetadataValue.text(status.start_time.isoformat()),
        "scheduler_duration_s":  MetadataValue.float(status.duration_seconds),
        "scheduler_log_url":     MetadataValue.url(status.log_url),
    })
```

### Status today (registry)

None of the trigger+monitor components are in the community registry yet — they've been roadmap items but lower priority than the kick-off-direction script (which covers the common case). If you need one for a specific scheduler, open an issue or contribute one — they're tractable (<200 LOC plus tests) once you have the vendor's REST API doc in hand.

For **Control-M** and **IBM TWS / z/OS**, the trigger+monitor capability is built as a separate paid offering and won't ship in the community registry. If that's your target, contact Dagster Labs for the commercial path. The observation-direction story for those is also commercial — same reasoning.

## Auth notes — local vs Dagster+

| Target | Auth | Header |
|---|---|---|
| Local OSS (`dg dev`) | none | (nothing — endpoint is open) |
| Dagster+ | user token from Settings → Tokens | `Dagster-Cloud-Api-Token: <token>` |
| Self-hosted with reverse proxy + auth | depends on your proxy | per your stack |

If you're testing against Dagster+ from your laptop, the same script works:
just set `DAGSTER_PLUS_USER_TOKEN` and point `DAGSTER_GRAPHQL_URL` at the
deployment. No code changes.

## What this demo covers vs the broader story

This demo is the case where customers say *"we already have a scheduler, we just need Dagster to be the executor"*. That's a simple problem — a documented `curl` script, no component required.

The other two directions are real but live in different components:

| Direction | Pattern | Where |
|---|---|---|
| Scheduler → Dagster (this demo) | `curl` to GraphQL | the `bin/kick_off_run.sh` script |
| Scheduler → runs job → Dagster watches | per-vendor observation sensor | see *Observability* section above |
| Dagster → triggers scheduler → waits → continues | per-vendor `*_job_asset` component | see *Trigger + monitor* section above |

Control-M and IBM TWS (and the broader z/OS workload-automation family) are reserved for the **commercial** offering across all three directions — the community registry stays clean of them. Every other scheduler in the table is community-fair-game.

## See also

<!-- TODO: link related walkthroughs -->
