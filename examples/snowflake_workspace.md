# Dagster + Snowflake — full-surface demo

A single bootstrap (**`seed.sh`** + **`bootstrap.sh`**) provisions a complete, always-running Snowflake data platform managed entirely from Dagster. Every Snowflake primitive — dynamic tables, tasks, stored procedures, streams, pipes, stages, materialized views, Iceberg tables, Cortex AI, Snowpark — appears as a first-class Dagster asset with real lineage, schedules, sensors, and event-driven automation. Push a button, see the whole graph light up.

## Quickstart

### Prerequisites

You need:

1. A **Snowflake account** (any tier — Standard works, Enterprise+ unlocks more features)
2. A **role** that can `CREATE DATABASE` (e.g. `ACCOUNTADMIN`, `SYSADMIN`) — or `seed.sh` falls back to "sandbox mode" inside an existing DB you own
3. An **auth method** — keypair (best), PAT, SSO browser, password+MFA, or plain password
4. (Optional) **AWS CLI authenticated** — if so, `seed.sh` auto-provisions an Iceberg external volume + S3-backed Snowpipe auto-ingest path

### Step 1 — Provision Snowflake (and optionally AWS)

`seed.sh` is interactive. It prompts for credentials, runs Day-0 governance probes (account network policy, default warehouse), detects object-name collisions, and then provisions everything. Writes `.env` for Step 2 to consume.

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/seed.sh -o seed.sh
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/seed.sql -o seed.sql
chmod +x seed.sh
./seed.sh
```

What it creates (full inventory in the [What this demo shows](#what-this-demo-shows) section):

- A `DAGSTER_DEMO` database with `RAW` / `STAGING` / `ANALYTICS` / `AI` schemas
- ~30 Snowflake-native entities: 5 dynamic tables (mixed `TARGET_LAG`), 6 tasks (incl. a parent→child chain + a config-schema launchpad task + a stream-consumer task), 3 stored procs (SQL + Snowpark Python), 2 CDC streams, 1 materialized view, 2 stages, 2 snowpipes (manual + auto-ingest), 1 alert, plus views/UDFs/tags/resource monitors
- A scoped `DAGSTER_RUNNER` role with only the grants Dagster needs (least-privilege runtime)
- (Optional, if AWS CLI is authed) S3 bucket + IAM role with the full Snowflake trust-policy dance, Iceberg `EXTERNAL VOLUME`, `STORAGE INTEGRATION`, and S3 PUT event notifications wired to the auto-ingest pipe's SQS queue

Idempotent. Takes 2–3 minutes. Re-runs with `--reset` if you want a clean slate.

### Step 2 — Scaffold the Dagster project

`bootstrap.sh` reads the `.env` written by `seed.sh`, creates a fresh project via `uvx create-dagster`, installs the community components, and writes `defs.yaml` files with cross-component dependencies wired in.

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/bootstrap.sh -o bootstrap.sh
chmod +x bootstrap.sh
./bootstrap.sh                              # comprehensive (default)
# OR ./bootstrap.sh --lean                  # minimum spine only
# OR ./bootstrap.sh --no-cortex --no-snowpark  # comprehensive minus AI bits
```

**Default is comprehensive** — every Snowflake capability the demo can exercise is on. Use `--lean` to strip back to just `snowflake_workspace` + Python ingest + Iceberg + freshness (the connected backbone), or `--no-<capability>` to turn individual add-ons off.

| Default on (turn off with `--no-*`) | Off by default (turn on with `--with-*`) |
|---|---|
| `--no-cortex` — `snowflake_cortex_asset` (SUMMARIZE on `AI.CUSTOMER_FEEDBACK`) | `--with-dbt` — official `dagster-dbt` integration (needs `dbt-snowflake` config) |
| `--no-snowpark` — `snowpark_pipeline` (Python in Snowflake) | |
| `--no-warehouse-pipeline` — Dagster-managed multi-step SQL | |
| `--no-observer` — `snowflake_table_observation_sensor` | |

### Step 3 — Run it

```bash
cd snowflake-demo
uv run dg dev                # auto-loads .env + .env.secrets from the project dir
```

Opens the UI at <http://localhost:3000>. You'll see ~30 assets in one connected lineage graph: RAW → DTs → tasks → marts → Iceberg/Cortex/Snowpark/warehouse_pipeline.

**Within ~5 minutes** of opening the page, schedules fire on their own — Dagster Python generators land orders/events into RAW, dynamic tables auto-refresh on `TARGET_LAG`, the DT-refresh sensor catches the row-count delta, `AutomationCondition.eager()` cascades downstream, S3 PUTs trigger Snowpipe loads, the observation sensor catches `COPY_HISTORY` rows and lights up the snowpipe asset. Nothing has to be clicked.

## What this demo shows

### What `seed.sh` creates in Snowflake

A database `DAGSTER_DEMO` with four schemas:

- **`RAW`** — source tables, populated by Dagster Python generators
  - `ORDERS`, `CUSTOMERS`, `PRODUCTS`, `EVENTS` (seeded empty, then continuously fed)
- **`STAGING`** — orchestratable Snowflake entities
  - **Stages**: `INTERNAL_STAGE` (internal), `LANDING_STAGE` (external on S3 with storage integration)
  - **Streams (CDC)**: `ORDERS_CDC_STREAM`, `CUSTOMERS_CDC_STREAM`
  - **Materialized view**: `CUSTOMER_LIFETIME_VALUE_MV` (pre-aggregated lifetime spend)
  - **Dynamic tables**: `PAID_ORDERS_DT` (5 min lag), `CUSTOMER_360_DT` (15 min), `TOP_PRODUCTS_DT` (1 hr), `HOURLY_ACTIVITY_DT` (1 min), `EVENTS_CLEANED_DT` (15 min)
  - **Snowpipes**: `ORDERS_MANUAL_INGEST_PIPE` (manual REFRESH), `ORDERS_AUTO_INGEST_PIPE` (AUTO_INGEST=TRUE, wired to SQS for S3 PUT events)
  - **Tasks**: `DAILY_ORDERS_ROLLUP`, `NIGHTLY_TIER_UPDATE_TASK` (root), `NIGHTLY_EVENTS_PURGE_TASK` (child, accepts `days_old` config), `PROCESS_ORDER_CHANGES_TASK` (drains the CDC stream), plus a few others
  - **Stored procedures**: `SP_RECOMPUTE_TIERS`, `SP_PURGE_OLD_EVENTS`, `SP_SNOWPARK_TOP_N` (Snowpark Python)
  - **Target table**: `ORDERS_INGESTED` (Snowpipe drops files here)
- **`ANALYTICS`** — downstream marts
  - `DAILY_REVENUE` (rolled up by `DAILY_ORDERS_ROLLUP`), `TOP_REVENUE_DAYS`, `ORDERS_CHANGELOG` (drained from stream)
- **`AI`** — Cortex source data
  - `CUSTOMER_FEEDBACK` text table + `CUSTOMER_FEEDBACK_SEARCH` Cortex Search service

**Outside Snowflake:** S3 bucket with IAM role + trust policy, storage integration, SQS notification wired into the bucket for the auto-ingest pipe, optional Iceberg external volume.

### What `bootstrap.sh` creates in Dagster

A `snowflake-demo` project that imports every Snowflake entity above as a Dagster asset, plus:

- **Python generators** (`synthetic_data_generator`) — `python_daily_orders`, `python_daily_events`, `python_hourly_orders_for_pipe`
- **Sinks**:
  - `orders_to_snowflake`, `events_to_snowflake` (`dataframe_to_snowflake` → `write_pandas`)
  - `orders_to_s3` (`dataframe_to_s3` → CSV with `{partition_key}-{run_id}` filenames so Snowpipe never dedupes)
- **Workspace component** — imports all Snowflake entities, generates two sensors (`snowflake_workspace_observation_sensor` for tasks + pipes, `snowflake_workspace_dt_refresh_sensor` for DTs)
- **Schedules** (`cron_schedule`):
  - `daily_orders_schedule`, `daily_events_schedule` — daily partition-driven
  - `hourly_orders_schedule` — hourly, drives the Snowpipe demo
  - `warehouse_observation_schedule` — every 5 min, observes streams/stages/alerts/raw_sources
- **Automation conditions** (`automation_condition_applicator`) — `eager` on tasks/procs/MVs/snowpipes/iceberg/warehouse_pipeline/snowpark/cortex; `on_cron` for root tasks and `cortex_feedback_summary`
- **Iceberg sink** (`snowflake_iceberg_table`) — `iceberg_daily_revenue`
- **Snowpark pipeline** — `snowpark_order_features`
- **Warehouse pipeline** (`warehouse_pipeline`) — `revenue_top_states` (SQLAlchemy-driven multi-step SQL DAG)
- **Cortex SUMMARIZE** — `cortex_feedback_summary`
- **Freshness check** — `freshness_daily_revenue`
- **Observation sensor** — `daily_revenue_observer` (table-watcher demo)
- **Raw source observables** — `raw_orders`, `raw_customers`, `raw_products`, `raw_events`, `ai_customer_feedback` — `@observable_source_asset` each polls row count + `last_altered`, emits a stable `data_version` so quiet observation ticks don't cascade downstream

### How data flows when it's running

```
                      ┌────────── Dagster schedules ──────────┐
                      │                                       │
   daily cron ──▶ python_daily_orders ──▶ orders_to_snowflake ──▶ RAW.ORDERS
   daily cron ──▶ python_daily_events ──▶ events_to_snowflake ──▶ RAW.EVENTS
   hourly cron ──▶ python_hourly_orders_for_pipe ──▶ orders_to_s3 ──▶ S3
                                                                  │
                                                                  ▼
                                          [S3 PUT] ──▶ SQS ──▶ ORDERS_AUTO_INGEST_PIPE
                                                                  │
                                                                  ▼
                                                       STAGING.ORDERS_INGESTED
                                                                  │
                                                       observation sensor catches
                                                       COPY_HISTORY row, emits
                                                       AssetMaterialization on
                                                       snowpipe_orders_auto_ingest_pipe

   RAW.ORDERS changes ──▶ PAID_ORDERS_DT, TOP_PRODUCTS_DT auto-refresh (TARGET_LAG)
                          DT-refresh sensor detects (rows changed) → emits Materialization
                                          │
                                          ▼
              eager AutomationCondition on task_daily_orders_rollup fires
                                          │
                                          ▼
                   EXECUTE TASK ──▶ ANALYTICS.DAILY_REVENUE (DELETE+INSERT, idempotent)
                                          │
                                          ▼ (eager cascade)
                                          │
            ┌──────────────┬──────────────┼─────────────────┬─────────────────┐
            ▼              ▼              ▼                 ▼                 ▼
    iceberg_daily_     revenue_top_   snowpark_order_   snowpark_top{3,    task_monthly_
       revenue           states          features        10,100}_           revenue_report
                                                       revenue_days

   RAW.CUSTOMERS changes ──▶ CUSTOMER_360_DT refreshes ──▶ HOURLY_CUSTOMER_METRICS,
                                                            WEEKLY_CHURN_SCORE eager-fire

   Stream observation: ORDERS_CDC_STREAM polled every 5 min, downstream
   PROCESS_ORDER_CHANGES_TASK eager-fires when has_data flips → drains the stream
   into ANALYTICS.ORDERS_CHANGELOG

   Nightly chain (every hour for demo): NIGHTLY_TIER_UPDATE_TASK ──▶ SP_RECOMPUTE_TIERS
                                                                    └──▶ NIGHTLY_EVENTS_PURGE_TASK
                                                                         (config_schema: days_old)
                                                                         └──▶ SP_PURGE_OLD_EVENTS

   Cortex (every 15 min): AI.CUSTOMER_FEEDBACK ──▶ CORTEX SUMMARIZE ──▶ cortex_feedback_summary
```

Everything propagates via Dagster's declarative automation framework — no manual orchestration, no glue scripts. Sensors emit stable `data_version`s so eager downstream only fires when something *actually* changed; quiet ticks don't cascade.

### Snowflake features showcased (booth checklist)

- ✅ Dynamic tables (5 of them, mixed cadences, both AUTO and INCREMENTAL refresh modes)
- ✅ Tasks (root + child chain, cron-driven, multi-statement with `BEGIN/END` blocks)
- ✅ Stored procedures (SQL + Snowpark Python, with `USING CONFIG` for per-call params)
- ✅ Streams (CDC capture + downstream consumer task pattern)
- ✅ Snowpipes (both manual and auto-ingest with SQS notifications)
- ✅ Stages (internal + external S3 with storage integration)
- ✅ Materialized views
- ✅ Iceberg tables (with external volume on S3)
- ✅ Cortex SUMMARIZE (and optionally Cortex Search)
- ✅ Snowpark Python procedures
- ✅ `PROGRAMMATIC_ACCESS_TOKEN` auth (PAT)
- ✅ Alerts (modeled as observable source asset)
- ✅ Multi-statement task bodies (Snowflake Scripting)
- ✅ Task config (`EXECUTE TASK ... USING CONFIG`, surfaced as a Dagster Config form in the launchpad)

### What a booth visitor sees

1. **Lineage diagram** — one connected DAG spanning RAW sources → DTs → tasks → marts → Iceberg/Cortex/Snowpark/warehouse_pipeline. Every node is real, every edge is real.
2. **Live animation** — within 5 minutes of opening the page, multiple assets refresh on their own as schedules fire, S3 files land, Snowpipe loads, DTs catch up, eager cascades trigger downstream. Nothing has to be clicked.
3. **Click any Snowflake asset** — see its native Snowflake metadata (`target_lag`, refresh mode, last `query_id`, row count, bytes, schedule, condition, etc.) populated from `SHOW` / `INFORMATION_SCHEMA` queries.
4. **Materialize the `nightly_events_purge_task` asset** — Dagster pops a typed form with `days_old: 90` defaulted; visitor can override to 7 or 365 and watch it execute with `USING CONFIG = '{"days_old": 7}'` against Snowflake.
5. **Drop a CSV in S3 manually** (or just wait for the hourly schedule) — within ~60 seconds, the Snowpipe sensor catches the COPY and `snowpipe_orders_auto_ingest_pipe` lights up green with file-level metadata in the materialization details.
6. **Failure stories work too** — kill a Snowflake task mid-run, the observation sensor records the failure state; rotate the PAT, materializations show the auth error in the UI without crashing the code location (bootstrap.sh sets safe env-var defaults).

## Why Dagster on top of Snowflake?

Two different conversations depending on the stack. Pick the one that fits.

### If you're a pure-Snowflake shop

Your entire data stack — ingestion, transforms, BI feeds — runs inside one account. You're not currently shopping for an orchestrator because Tasks + Dynamic Tables + Alerts already work. The honest pitch for Dagster here is **not** cross-tool lineage (you don't have other tools). It's that **Snowflake schedules; Dagster *reacts*** — and the reactive patterns you actually want often don't fit Snowflake's narrow trigger surface.

Snowflake's native trigger surface:
- **Tasks** — cron schedules, or `AFTER` another task in a chain
- **Snowpipe auto-ingest** — S3 / GCS / Azure object PUTs to a configured stage
- **Alerts** — scheduled polling with one conditional action

Common patterns this misses, even when nothing leaves Snowflake:

| Reactive pattern | Native Snowflake answer | Dagster answer |
|---|---|---|
| "When `RAW.ORDERS` gains > 1000 new rows, kick off downstream rollups." | Alert detects it, but firing the next task means INSERTing into a coordination table that ANOTHER scheduled task polls. | One-line `snowflake_table_observation_sensor` watching ORDERS' row count → materializes downstream assets directly. |
| "When *both* `customer_360_dt` and `paid_orders_dt` finish their refreshes, fire the joined rollup." | No native multi-DT-completion trigger. Workaround: schedule the rollup on a long enough lag that *probably* both finished. | `AutomationCondition.eager()` on the downstream asset — fires the moment all upstreams have materialized, not a moment later. |
| "When a webhook arrives from Stripe / HubSpot / a custom app, ingest payload → trigger downstream procs." | External Functions can be called *from* a task, but something outside Snowflake still has to receive the webhook and trigger the task with the payload. | Dagster webhook sensor receives, lands the payload, triggers downstream assets keyed by the event. |
| "Backfill the last 30 days of `DAILY_ORDERS_ROLLUP` in parallel, 5 at a time, skipping already-materialized partitions." | Custom procedure that loops over a date range, calls `EXECUTE TASK ... WITH ARGUMENTS`. Skip-if-exists is DIY. | Partitioned asset + `dg launch --partition-range 2026-04-20...2026-05-19 --max-concurrent 5`. Done. |
| "If the alert fires *and* it's business hours, page on-call. Otherwise, log and retry off-peak." | Alerts run one action. Branching = a second alert + careful conditions. | `AutomationCondition` composition: `eager() & on_cron(business_hours)`, with a separate path for off-hours. |
| "Replay this one task for last Tuesday only." | `ALTER TASK ... EXECUTE` with arguments — but the task body has to be parameterized for the date. Hope you wrote it that way. | Click the partition in the UI. Or `dg launch --partition 2026-05-13`. The asset's body sees the partition key automatically. |

Plus the things that aren't trigger-shaped but still help even in a pure-Snowflake setup:

| | Native Snowflake | Dagster + Snowflake |
|---|---|---|
| **Asset-level data quality** | Tasks-as-tests; coordinating "block downstream on failure" is DIY | `@asset_check` runs inline; pass/fail in the same UI; blocks downstream automatically |
| **Per-asset metadata + history** | `TASK_HISTORY` view, query-centric (duration, bytes) | Per-asset: schema, row counts, freshness, preview, code-version, materialization history — the lens is the table, not the task that wrote it |
| **Local development** | Can't run a task outside the account; iteration loop = edit → push → wait for the next scheduled run | `dg dev` runs the full graph locally, against either a dev account or a DuckDB stand-in |
| **Branching + preview deploys** | No native git-style branching for orchestration | Dagster+ branch deployments — every PR gets an isolated environment |
| **Day-2 ops** | Per-task suspend/resume; ALTER TASK for changes | Bulk suspend/resume, freeze windows, audit log of who-materialized-what, alerting hooks (Slack / PagerDuty / email) |

**When to walk away from this conversation:** if the team's trigger needs really are just "nightly cron + AFTER chains + Snowpipe on file land" *and* there's no near-term partition-replay, webhook-trigger, or external-event story — native scheduling is fine. Save the migration energy.

### If you're a heterogeneous stack (most teams)

Snowflake is one node in your data graph. You also have Fivetran / Airbyte / Sling for ingest, dbt for transforms, BI tools like Tableau / Power BI / Looker reading downstream, maybe a reverse-ETL step pushing data to Salesforce / HubSpot, ML models scoring upstream of all of it, and Python jobs gluing the edges. The pitch is straightforward: **one asset graph for the whole flow.**

| | Snowflake-native scheduling | Dagster + Snowflake |
|---|---|---|
| **Lineage** | Stops at the Snowflake boundary | One graph end-to-end: SaaS ingest → Snowflake tables → tasks / DTs / procs → dbt → BI refreshes → reverse-ETL → ML. Click any node, see every upstream and downstream. |
| **Cross-tool triggers** | External Functions can call out, but the trigger model is still cron-centric | Sensors on file landings, message-queue events, webhook arrivals, OTHER systems' completions; `AutomationCondition` reacts to any upstream asset, wherever it lives |
| **Heterogeneous compute** | Cross-tool work means External Functions, custom polling, or a third-party orchestrator on the side | Snowflake + Databricks + BigQuery + Postgres + S3 + Kafka + Python tasks in one project, deps between them, retries + alerting per-asset |
| **Failure surfaces** | Each tool has its own history view. Cross-tool root-causing means joining timelines by hand | Unified timeline. A failed Snowflake task fails *in Dagster* with full upstream and downstream context across every tool involved |
| **Partition coordination** | Each tool has its own backfill story (or doesn't) | One partition scheme spans the graph — backfilling "yesterday" replays Fivetran sync → Snowflake task → dbt model → BI refresh as one coordinated set |
| **Branch deploys** | DIY per tool | Dagster+ branch environments — a single PR brings up a sandbox that touches every system in the graph |
| **All the pure-Snowflake stuff above** | — | Yes, you get this too |

The reactive-pattern table from the pure-Snowflake section still applies here — and gets *more* valuable, because the upstream signal often lives outside Snowflake (a Kafka topic, an S3 drop, a SaaS webhook). Dagster collapses both the cross-tool and the in-Snowflake reactive cases into the same primitive: `AutomationCondition`.

### When Dagster is genuinely overkill

For either audience, Dagster adds moving parts you don't need if:

- Your trigger needs really are just nightly cron + AFTER chains + cloud-storage auto-ingest
- You're never going to do non-trivial backfills (no "replay these 17 days, 5-at-a-time" requests)
- You don't have external event sources or webhooks driving anything
- You don't have downstream tools whose state you need to coordinate with Snowflake's

Most teams find at least one of these breaks within a quarter or two. But if all four hold for you, native Snowflake scheduling is the right answer and a worth-it cost in saved complexity.

## `seed.sh` flags

```bash
./seed.sh                          # defaults: interactive, Iceberg auto-on if AWS authed
./seed.sh --demo-account           # auto-fix Day-0 governance gaps (only against your own demo account)
./seed.sh --with-iceberg=false     # skip Iceberg even if AWS CLI is authed
./seed.sh --with-iceberg           # require Iceberg (fail if AWS CLI missing)
./seed.sh --target-db MY_DEMO      # use a different database name (default: DAGSTER_DEMO)
./seed.sh --reset                  # drop and recreate target database
./seed.sh --no-dagster-runner      # skip creating the scoped DAGSTER_RUNNER role
./seed.sh --runner-role MY_ROLE    # use a different name for the scoped role
./seed.sh --bucket my-iceberg-bkt  # override S3 bucket name
./seed.sh --iam-role my-sf-role    # override IAM role name
./seed.sh --volume-name MY_VOL     # override Snowflake external volume name
./seed.sh --help                   # full help
```

## `bootstrap.sh` flags

```bash
./bootstrap.sh                                          # comprehensive demo (default)
./bootstrap.sh --lean                                   # minimum connected spine only
./bootstrap.sh --no-cortex --no-snowpark                # comprehensive minus AI bits
./bootstrap.sh --name my-snow-demo                      # name the project (default: prompts)
./bootstrap.sh --env-file ./.env.demo                   # use a specific env file
./bootstrap.sh --with-dbt                               # include the dagster-dbt integration
./bootstrap.sh --help                                   # full help
```

The default produces ~30 assets. `--lean` strips back to: `python_daily_events` → `events_to_snowflake` → `snowflake_workspace` imports → `iceberg_daily_revenue` (if Iceberg available) → `freshness_daily_revenue`. Useful for a quick check that the end-to-end wiring works before turning on all the add-ons.

## A note on OpenFlow

**Dagster can orchestrate OpenFlow. `seed.sh` cannot pre-build OpenFlow flows for you.**

OpenFlow (Snowflake's NiFi-based data integration service) is a different kind of object from Tasks / Dynamic Tables / Procedures: it isn't created with a SQL DDL statement, there's no `snowflake_openflow_flow` Terraform resource yet, and the BYOC runtime itself is a non-trivial EKS-cluster-in-your-cloud deployment. The IaC story for OpenFlow flows today is *"design in the UI → export as a JSON process-group bundle → commit to Git → import via the NiFi REST API."* That's real, but bootstrapping a runnable OpenFlow runtime + importing the JSON is heavier than `seed.sh` can do.

**What that means for live demos:**

- If you're showing this against a Snowflake account that already has OpenFlow flows configured, set `import_openflow_flows: true` in the workspace `defs.yaml`. The workspace component discovers them via the existing OpenFlow telemetry surface and they show up as observable Dagster assets — same as tasks / dynamic tables / streams.
- If you don't have OpenFlow set up yet and want to show the integration on stage, pre-build one flow in the UI ahead of time and have it live in the demo account. `seed.sh` populates the *other* nine entity types so the discovery story still lands.
- If you want to skip OpenFlow on stage, leave `import_openflow_flows: false` (the demo's default) and the absence is invisible.

The companion doc [snowflake_demo_account_requirements.md](snowflake_demo_account_requirements.md) covers everything else your account needs (or doesn't) to light up the full demo.

## Running this against a corporate Snowflake account (the SE reality)

If you're a Dagster SE — or any data engineer running this against a shared corporate Snowflake — your role is almost certainly some flavor of `SANDBOX_WRITER` / `DEVELOPER` / per-team writer, **not** `ACCOUNTADMIN` / `SECURITYADMIN` / `USERADMIN`. That makes the "default" auth choice surprising. Here's the quick triage:

| Your permissions | Easiest auth | Why |
|---|---|---|
| **Can run `ALTER USER … SET RSA_PUBLIC_KEY=…` on yourself** (rare — needs OWNERSHIP on your user) | **Keypair** | Headless, works with the Dagster daemon, no browser. `seed.sh`'s default. |
| **Can create a PAT *and* there's already a permissive network policy attached to your user** | **PAT** | Headless. But: PATs require an associated network policy, which usually only `SECURITYADMIN` can create/attach. If your account doesn't already have one, you'll hit `Programmatic access token failed authentication. No active network policy found …` and need to fall back. |
| **None of the above** (the common case for SEs) | **SSO (externalbrowser)** | Works on any account that allows SSO. Browser tab pops once per session and the token is cached in your OS keychain via `keyring`. **Caveat:** sensors + scheduled runs (the Dagster daemon) can't open a browser, so `bootstrap.sh`'s schedules won't fire on the daemon until you re-auth. For a stage demo / `dg dev` exploration this is fine. |
| **No keypair, no PAT, no SSO** | **Password** | If your account still allows it. Most don't. |

**The honest path for most SEs is SSO + sandbox mode** — see the next subsection.

### What "sandbox mode" means

`seed.sh` auto-detects when the target database **already exists and you don't own it** and **strips the DDL it can't run as your role**:

- `CREATE DATABASE` → skipped (you don't have it)
- `USE ROLE SYSADMIN` → skipped (you can't switch to roles you don't have)
- `RAW` / `STAGING` / `ANALYTICS` / `AI` schemas → renamed to `DAGSTER_DEMO_RAW` / `_STAGING` / `_ANALYTICS` / `_AI` (so the seed doesn't collide if 14 other people are also running this against the shared `SANDBOX`)
- `COMPUTE_WH` → string-substituted with whatever warehouse you actually have USAGE on

The end state is identical (tasks, DTs, procs, streams, pipes, alerts) — just scoped to schemas your role *owns* in a database you already control. Run `seed.sh`, then point `bootstrap.sh` at `SANDBOX.DAGSTER_DEMO_STAGING` and you get the full discovery story without ever needing `ACCOUNTADMIN`.

### What you **won't** be able to do as a sandboxed role

Be honest with the customer about these — they're real limits of running stage demos against a shared corporate account, not Dagster limits:

- **`ALTER TASK … RESUME`** needs `EXECUTE TASK` on the account. Most sandboxed roles don't have it, so tasks materialize as suspended. Tasks can still be run on demand from Dagster's UI (click-to-materialize calls `EXECUTE TASK <name>`) — but they won't auto-fire on their cron schedule.
- **Materialized views** require Snowflake Enterprise edition; Standard accounts fail the MV creation in `seed.sql`. Non-blocking — the seed continues and 49/50 statements still land.
- **Snowpipe auto-ingest** needs a stage with a notification integration, which is usually `ACCOUNTADMIN`-only to create. `seed.sh` creates the pipe in `MANUAL` mode (PUT-then-COPY); the auto-ingest path runs only when AWS is authenticated AND your role can `CREATE STORAGE INTEGRATION`.
- **Iceberg `EXTERNAL VOLUME`** needs `ACCOUNTADMIN`. If you can't reach that role, pass `--with-iceberg=false` and the demo runs without it.
- **`CREATE EXTERNAL VOLUME` / `CREATE NOTIFICATION INTEGRATION` / `CREATE CORTEX SEARCH SERVICE`** all gate on `ACCOUNTADMIN` or specific privilege grants — full table in [snowflake_demo_account_requirements.md](snowflake_demo_account_requirements.md).

For a customer demo on a customer-owned account, none of these apply — they'll run as a powerful enough role to use everything. The above is purely about doing dry-runs on your own employer's locked-down account.

## What gets generated

```
snowflake-demo/
├── .env -> ../.env                           # symlinked to seed.sh's output
├── .env.secrets                              # mode 600, gitignored, holds secrets
├── pyproject.toml                            # snowflake-connector-python pinned + extras
└── src/snowflake_demo/
    ├── components/                           # community component sources installed by bootstrap.sh
    │   ├── snowflake_workspace/
    │   ├── synthetic_data_generator/
    │   ├── dataframe_to_snowflake/
    │   ├── dataframe_to_s3/                   (if S3_STAGING_BUCKET set by seed.sh)
    │   ├── snowflake_iceberg_table/           (if Iceberg available)
    │   ├── snowflake_cortex_asset/            (--with-cortex / default on)
    │   ├── snowpark_pipeline/                 (--with-snowpark / default on)
    │   ├── warehouse_pipeline/                (--with-warehouse-pipeline / default on)
    │   ├── snowflake_table_observation_sensor/(--with-observer / default on)
    │   ├── freshness_check/
    │   ├── cron_schedule/
    │   └── automation_condition_applicator/
    ├── definitions.py                        # wires automation_condition_applicator + safe env defaults
    └── defs/
        ├── raw_sources/                      # @observable_source_asset for RAW.*/AI.*
        ├── python_daily_orders/              # synthetic_data_generator (orders)
        ├── orders_to_snowflake/              # dataframe_to_snowflake → RAW.ORDERS
        ├── python_daily_events/              # synthetic_data_generator (events)
        ├── events_to_snowflake/              # dataframe_to_snowflake → RAW.EVENTS
        ├── python_hourly_orders_for_pipe/    # hourly orders generator (Snowpipe demo)
        ├── orders_to_s3/                     # dataframe_to_s3 → triggers ORDERS_AUTO_INGEST_PIPE
        ├── snowflake_workspace/              # imports all 30+ entities + assets_by_name deps
        ├── iceberg_daily_revenue/            # snowflake_iceberg_table sink
        ├── cortex_feedback_summary/          # SUMMARIZE on AI.CUSTOMER_FEEDBACK
        ├── snowpark_order_features/          # Snowpark Python in Snowflake
        ├── revenue_top_states/               # warehouse_pipeline multi-step SQL DAG
        ├── daily_revenue_observer/           # observation sensor demo
        ├── freshness_daily_revenue/          # freshness_check
        ├── daily_orders_schedule/            # cron_schedule
        ├── daily_events_schedule/
        ├── hourly_orders_schedule/
        ├── warehouse_observation_schedule/
        └── automation_conditions/            # automation_condition_applicator rules
```

## Switching auth post-bootstrap

The `.env` written by `seed.sh` carries one auth method. To switch, edit `.env` (or `.env.secrets` for the actual secret value), then re-run `bootstrap.sh` (or just `uv run dg dev` if the YAML field shape is the same). Each component's `defs.yaml` references env vars via the `_env_var` convention, so swapping the env value is enough.

For PAT → keypair (or vice versa), you may need to edit the `authenticator:` field in `snowflake_workspace/defs.yaml` and friends — `bootstrap.sh` picks one shape based on `SNOWFLAKE_AUTH_METHOD` at scaffold time.

## Teardown

To rebuild from scratch:

```bash
./seed.sh --reset                  # drops + recreates DAGSTER_DEMO in Snowflake
```

Iceberg-related AWS resources (S3 bucket, IAM role) are NOT auto-deleted by `--reset` — they're idempotent and cheap to keep around. To delete:

```bash
aws s3 rm s3://<bucket>/ --recursive
aws s3api delete-bucket --bucket <bucket>
aws iam delete-role-policy --role-name <role> --policy-name iceberg-s3-access
aws iam delete-role --role-name <role>
```

## Layer in more Snowflake components

The community registry ships a wide Snowflake surface. Compose them as needed:

| Component | What it does |
|---|---|
| `dataframe_to_snowflake` | Write a pandas/polars DataFrame to a Snowflake table (`write_pandas`) |
| `dataframe_to_snowflake_bulk` | Bulk-load (PUT + COPY INTO) for large frames |
| `external_snowflake_table` | Declare-only asset (lineage without management) |
| `snowflake_resource` | Shared connection resource for hand-written assets |
| `snowflake_io_manager` | Pandas DataFrames → table per asset, automatically |
| `snowflake_polars_io_manager` | Same, polars DataFrames |
| `snowflake_pyspark_io_manager` | Same, PySpark DataFrames |
| `snowflake_table_observation_sensor` | Watch a table for new rows / row count changes |
| `snowflake_access_history_ingestion` | Pull `ACCOUNT_USAGE.ACCESS_HISTORY` into Dagster |
| `snowflake_cortex_asset` | Call Snowflake Cortex LLMs as a Dagster asset |
| `warehouse_pipeline` | Multi-step CTE pipeline (Snowflake dialect supported) |
| `snowpark_pipeline` | Snowpark DataFrame multi-step pipeline |
| `snowflake_iceberg_table` | Iceberg table on external volume |

Add any of them with:

```bash
uvx --from dagster-community-components-cli dagster-component add <name>
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `seed.sh` aborts at "Role 'X' isn't visible" | Your user can't see that role. Re-run and pick a visible one from the list. |
| `seed.sh` says warehouse missing → offers to create | Pick `c` to create it (XSMALL, AUTO_SUSPEND=60s, AUTO_RESUME=TRUE — effectively free when idle). |
| `bootstrap.sh` says "expected seed objects not found" | Run `seed.sh` first against the same account/database. |
| `uv run dg dev` startup logs warn about unset SNOWFLAKE_PAT / PASSWORD | The project loads anyway (UI renders). Materializations fail until you export the var or add it to `.env.secrets`. |
| `dg check defs` fails on `warehouse_pipeline` with `SNOWFLAKE_URL` unset | `bootstrap.sh`'s `definitions.py` builds `SNOWFLAKE_URL` from `SNOWFLAKE_PAT` + account + user + db. If PAT isn't set, the URL won't build. |
| Iceberg verification fails after 80s of retries | IAM trust-policy propagation can occasionally take longer. AWS resources are intact — re-verify manually: `USE ROLE ACCOUNTADMIN; SELECT SYSTEM$VERIFY_EXTERNAL_VOLUME('DAGSTER_DEMO_VOLUME');` |
| Snowpipe doesn't fire on S3 PUT | Verify the S3 bucket notification config — `aws s3api get-bucket-notification-configuration --bucket <bucket>` should show a `QueueConfigurations` entry pointing at the pipe's SQS ARN. |
| `EVENTS_CLEANED_DT` shows `invalid type for dimension "timestamp"` | Old issue from pre-v0.10.2 setups — re-seed; `seed.sql`'s current shape casts the column to `TIMESTAMP_NTZ` before any hypertable / DT logic depends on it. |

## See also

- [`snowflake_demo_account_requirements.md`](snowflake_demo_account_requirements.md) — full account-permission + product-tier matrix, plus a paste-ready ask for your Snowflake partnership contact
- [`snowflake_single_entity.md`](snowflake_single_entity.md) — minimal companion walkthrough: one entity, one asset, no add-ons
- [`snowflake_iceberg_databricks.md`](snowflake_iceberg_databricks.md) — same Iceberg story, but read from Databricks instead of Snowflake
- [`snowpark_pipeline.md`](snowpark_pipeline.md) — Snowpark-specific walkthrough
- Per-component READMEs in the [templates repo](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets) for every Snowflake component listed above
