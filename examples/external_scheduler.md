# External Scheduler demo

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

The script returns 0 only on `LaunchRunSuccess` *and* a `success` final run
status. That's how the scheduler knows whether to retry, alert, or chain
forward.

## Auth notes — local vs Dagster+

| Target | Auth | Header |
|---|---|---|
| Local OSS (`dg dev`) | none | (nothing — endpoint is open) |
| Dagster+ | user token from Settings → Tokens | `Dagster-Cloud-Api-Token: <token>` |
| Self-hosted with reverse proxy + auth | depends on your proxy | per your stack |

If you're testing against Dagster+ from your laptop, the same script works:
just set `DAGSTER_PLUS_USER_TOKEN` and point `DAGSTER_GRAPHQL_URL` at the
deployment. No code changes.

## What this isn't

This isn't a "let Dagster trigger Control-M" demo — that's the reverse
direction (Dagster orchestrates external systems) and is its own commercial
integration. This demo is the case where customers say *"we already have a
scheduler, we just need Dagster to be the executor"*. That's a much simpler
problem and doesn't need a component at all — just a documented script.
