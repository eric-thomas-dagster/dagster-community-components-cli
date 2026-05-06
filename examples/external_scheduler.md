# External Scheduler demo

How to keep an existing master scheduler — Control-M, Autosys, Tidal, cron,
Jenkins, anything — in charge of *when* a Dagster job runs, without writing
a Dagster integration component for it.

The scheduler stays the source of truth for cadence, retries, and SLA
alerting. Dagster owns *how* the work runs (lineage, asset graph, execution).
The bridge between them is a shell script that calls Dagster's GraphQL API.

```
   external scheduler  ──────┐
   (Control-M, Autosys,      │ shells out
   cron, Jenkins, ...)       ▼
                          bin/kick_off_run.sh
                              │ POST /graphql  (curl)
                              ▼
                          ┌──────────────────────────────┐
                          │  daily-partitioned Dagster   │
                          │  job:                         │
                          │    csv → summarize → csv      │
                          └──────────────────────────────┘
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

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `csv_file_ingestion` | ingestion | Read partitioned source CSV |
| 2 | `summarize` | transformation | Per-partition group-by aggregate |
| 3 | `dataframe_to_csv` | sink | Write `daily_revenue_{partition_key}.csv` |

The Dagster project is a normal partitioned pipeline. Nothing in it knows
or cares about the external scheduler — that's the design.

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
Control-M:  job step calls   bin/kick_off_run.sh %%$ODATE
Autosys:    command:         bin/kick_off_run.sh $$AUTODATE
cron:       crontab line:    0 2 * * *  cd /opt/proj && bin/kick_off_run.sh
Jenkins:    shell step:      sh "bin/kick_off_run.sh ${BUILD_DATE}"
```

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
