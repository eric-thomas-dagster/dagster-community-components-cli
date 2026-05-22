# Dagster + Snowflake — booth demo

Two scripts. Five minutes. A fully-orchestrated Dagster project against Snowflake.

## Quickstart

### Prerequisites

You need:

1. A **Snowflake account** (any tier — Standard works, Enterprise+ unlocks more features)
2. A **role** that can `CREATE DATABASE` (e.g. `ACCOUNTADMIN`, `SYSADMIN`) — or the seed silently falls back to "sandbox mode" inside an existing DB you own
3. An **auth method** — keypair (best), PAT, SSO browser, password+MFA, or plain password

### Step 1 — Seed Snowflake with realistic stuff to orchestrate

Creates a `DAGSTER_DEMO` database with ~30 entities across `RAW` / `STAGING` / `ANALYTICS` / `AI` schemas:

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_snowflake_environment.sh -o setup_snowflake_environment.sh
chmod +x setup_snowflake_environment.sh
./setup_snowflake_environment.sh
```

Tasks, dynamic tables, stored procedures (incl. Snowpark Python), streams, snowpipes, alerts, materialized view, Cortex Search service, Hybrid table, views, UDFs, tags, resource monitor — every Snowflake primitive that can be created via SQL. Idempotent. Takes 2-3 minutes.

### Step 2 — Scaffold the Dagster project

Auto-detects what's available on your account (Iceberg volumes, Cortex services, account edition) and only scaffolds components that can actually materialize:

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_snowflake_workspace_demo.sh -o setup_snowflake_workspace_demo.sh
chmod +x setup_snowflake_workspace_demo.sh

export WANT_EVERYTHING=true   # auto-accept all add-on prompts
./setup_snowflake_workspace_demo.sh
```

### Step 3 — Run it

```bash
cd snowflake-dagster
source .env.demo
uv run dg dev
```

Opens the UI at <http://localhost:3000>. You'll see an asset graph spanning the imported Snowflake entities + Dagster's orchestration overlay (warehouse pipelines, Snowpark pipelines, Cortex assets, partitioned chains, the new Snowpipe load sensor, time-travel queries, Iceberg, dbt, etc).

### After it works — what to try

1. **Click an imported task** and materialize it — runs `EXECUTE TASK` server-side
2. **Click `regional_top_paid_pipeline`** — multi-step SQL pipeline running joins + commission calc + multi-sink, all pushed down to Snowflake
3. **Click `cortex_demo`** — calls `SNOWFLAKE.CORTEX.COMPLETE` and lands the LLM output as an asset
4. **Click `cortex_search_results`** — queries your seeded Cortex Search Service
5. **Click `python_daily_events` → backfill 30 days** — partitioned Python → Snowflake landing chain, replays in parallel

If any of this breaks, [Troubleshooting](#troubleshooting) is at the bottom of this doc.

## Why Dagster on top of Snowflake?

Two different conversations depending on the stack. Pick the one that fits.

---

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

---

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

---

### When Dagster is genuinely overkill

For either audience, Dagster adds moving parts you don't need if:

- Your trigger needs really are just nightly cron + AFTER chains + cloud-storage auto-ingest
- You're never going to do non-trivial backfills (no "replay these 17 days, 5-at-a-time" requests)
- You don't have external event sources or webhooks driving anything
- You don't have downstream tools whose state you need to coordinate with Snowflake's

Most teams find at least one of these breaks within a quarter or two. But if all four hold for you, native Snowflake scheduling is the right answer and a worth-it cost in saved complexity.

## Don't have a populated Snowflake account yet?

There's a companion seed script that creates a realistic "before" state — a `DAGSTER_DEMO` database with `RAW.{ORDERS,CUSTOMERS,PRODUCTS,EVENTS}` (seeded with synthetic data) and a `STAGING` schema populated with **6 tasks**, **4 dynamic tables**, **3 stored procedures** (including one Snowpark Python proc), **2 streams**, **1 materialized view**, **2 stages**, **1 snowpipe**, and **1 alert**. Idempotent. Run it once on a demo account, then point the workspace setup at `DAGSTER_DEMO.STAGING` for a discovery output that's genuinely impressive:

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_snowflake_environment.sh -o setup_snowflake_environment.sh
chmod +x setup_snowflake_environment.sh
./setup_snowflake_environment.sh
```

Teardown with [`teardown_snowflake_environment.sql`](teardown_snowflake_environment.sql) — drops the whole `DAGSTER_DEMO` database.

**Safety guards before the seed runs anything:**

The bash wrapper does three pre-flight checks against your account *before* any DDL fires:

1. **Role + warehouse exist** and are visible to the connected user. If `SHOW ROLES LIKE '<role>'` or `SHOW WAREHOUSES LIKE '<wh>'` returns nothing, it aborts with a clear error instead of failing opaquely on the first `USE` statement.
2. **Target database name is configurable** — prompted at startup (default `DAGSTER_DEMO`, override with the `SNOWFLAKE_TARGET_DATABASE` env var). The bash wrapper string-substitutes that name into all 32 SQL references before executing, so you can stage the seed into an isolated `DAGSTER_DEMO_$(whoami)` if you're sharing the account.
3. **Object-level collision inventory** — if the target database already exists, the wrapper queries `INFORMATION_SCHEMA.{TABLES, TASKS, DYNAMIC_TABLES, PROCEDURES, VIEWS}` + `SHOW {STREAMS, PIPES, STAGES, ALERTS}` for every name the seed is about to `CREATE OR REPLACE`. Prints a manifest of any overlap and asks: **[r]euse and overwrite** / **[d]rop database first and recreate** / **[c]hange to a different name** / **[q]uit**.

Net effect: you can't accidentally clobber production work by running this on the wrong account. If anything's off, the wrapper surfaces it before issuing any DDL.

### A note on OpenFlow

**Dagster can orchestrate OpenFlow. We can't pre-build OpenFlow flows in this seed script.**

OpenFlow (Snowflake's NiFi-based data integration service) is a different kind of object from Tasks / Dynamic Tables / Procedures: it isn't created with a SQL DDL statement, there's no `snowflake_openflow_flow` Terraform resource yet, and the BYOC runtime itself is a non-trivial EKS-cluster-in-your-cloud deployment. The IaC story for OpenFlow flows today is *"design in the UI → export as a JSON process-group bundle → commit to Git → import via the NiFi REST API."* That's real, but bootstrapping a runnable OpenFlow runtime + importing the JSON is heavier than a one-script demo can do.

**What that means for live demos:**

- If you're showing this against a Snowflake account that already has OpenFlow flows configured, set `import_openflow_flows: true` in the workspace `defs.yaml`. The workspace component discovers them via the existing OpenFlow telemetry surface and they show up as observable Dagster assets — same as tasks / dynamic tables / streams. **Dagster orchestrates them; lineage extends through them.**
- If you don't have OpenFlow set up yet and want to show the integration on stage, pre-build one flow in the UI ahead of time and have it live in the demo account. The seed script populates the *other* nine entity types so the discovery story still lands.
- If you want to skip OpenFlow on stage, set `import_openflow_flows: false` (the demo's default) and the absence of flows in `dg dev` will be invisible.

### Setting up an OpenFlow flow before your demo (~15 min)

If you want the OpenFlow part of the story to land on stage, the only path today is to pre-build a flow in your demo account *before* the talk. Here's the shortest path:

**1. Verify the runtime exists.** OpenFlow is **BYOC** (deployed in your own cloud) and gated per Snowflake account. Sign in to **Snowsight → Data → Integrations → OpenFlow**. If you see a "Deploy runtime" button instead of a list of runtimes, the BYOC deployment hasn't been run yet and you'll need to coordinate with whoever runs cloud infra (it provisions an EKS cluster in your AWS account — a several-hour task, not a 15-minute one). If you see at least one runtime listed, you're good to proceed.

**2. Pick a pre-built connector instead of designing from scratch.** Snowflake ships ~12 connectors as one-click installs from a marketplace-like UI:
   - **Database CDC** — Postgres, MySQL, MSSQL, MongoDB, SQL Server (CDC)
   - **SaaS** — Salesforce, Box, Slack, SharePoint, Google Drive, Workday
   - **Messaging** — Kafka, Kinesis

   For a quick demo, **Postgres CDC** is the cleanest story (you can use a free-tier Supabase or RDS as the source) — it lands a stream of changes into Snowflake tables in real time. **Slack** is the second-easiest (no source DB needed, but requires a Slack workspace + bot token).

**3. Install the connector.** In Snowsight: **Data → Integrations → OpenFlow → Connectors → Add Connector**. Click your chosen connector → "Install". Snowflake handles the runtime side; you fill in:
   - Source credentials (Postgres connection string / Slack bot token / etc.)
   - Snowflake target — database, schema, warehouse, role
   - Sync mode — full refresh / incremental / CDC
   - Schedule (some connectors are continuous, some scheduled)

**4. Set up a Snowflake Service User for the connector.** OpenFlow connectors authenticate to Snowflake as a service user, not as your interactive user. From a worksheet:
   ```sql
   CREATE USER IF NOT EXISTS OPENFLOW_SERVICE_USER
     TYPE = SERVICE
     DEFAULT_ROLE = OPENFLOW_LOADER
     DEFAULT_WAREHOUSE = COMPUTE_WH;

   CREATE ROLE IF NOT EXISTS OPENFLOW_LOADER;
   GRANT USAGE ON DATABASE DAGSTER_DEMO TO ROLE OPENFLOW_LOADER;
   GRANT USAGE ON SCHEMA DAGSTER_DEMO.RAW TO ROLE OPENFLOW_LOADER;
   GRANT CREATE TABLE, MODIFY ON SCHEMA DAGSTER_DEMO.RAW TO ROLE OPENFLOW_LOADER;
   GRANT ROLE OPENFLOW_LOADER TO USER OPENFLOW_SERVICE_USER;

   -- Register an RSA public key on the service user (run from a key you generated):
   ALTER USER OPENFLOW_SERVICE_USER SET RSA_PUBLIC_KEY = '<pubkey contents>';
   ```
   Service users **must** use keypair auth — passwords aren't allowed. Paste the matching private key into the connector's credential field when you install it.

**5. Run a sync.** The connector UI has a "Run" button — click it. After a minute, you should see new tables under `DAGSTER_DEMO.RAW` (or wherever you pointed it). If it errors, the UI shows the NiFi processor that failed with a stack trace; usually it's a missing grant on the target schema.

**6. Re-run the Dagster workspace setup with OpenFlow on.** Edit the workspace `defs.yaml` (or re-run the script and `[r]euse`-overwrite):
   ```yaml
   attributes:
     # ... existing fields ...
     import_openflow_flows: true
   ```
   Then `uv run dg check defs` followed by `uv run dg dev`. Your OpenFlow flow shows up in Dagster's asset graph alongside tasks / dynamic tables / procs / etc. — Dagster materializes via `EXECUTE FLOW`, lineage flows through, run history streams in.

**For a really compelling demo:** wire one of your imported `RAW.*` tables (the one OpenFlow lands into) as the upstream of the `warehouse_pipeline` add-on. Now the story is end-to-end: OpenFlow ingests CDC from Postgres → lands in `RAW.ORDERS` → Dagster sees the row-count change → triggers `warehouse_pipeline` via `AutomationCondition.eager()` → joins + commission + multi-sink, all in Snowflake compute. One asset graph, every Snowflake-native primitive doing its best job.

**If you can't get OpenFlow set up in time**, drop it. The seed script's tasks + dynamic tables + procs + streams + pipes + alerts cover 90% of the "Dagster orchestrates everything Snowflake-native" story; OpenFlow is the cherry, not the cake.

## Running this against a corporate Snowflake account (the SE reality)

If you're a Dagster SE — or any data-engineer running this against a shared corporate Snowflake — your role is almost certainly some flavor of `SANDBOX_WRITER` / `DEVELOPER` / per-team writer, **not** `ACCOUNTADMIN` / `SECURITYADMIN` / `USERADMIN`. That makes the "default" auth choice surprising. Here's the quick triage:

| Your permissions | Easiest auth | Why |
|---|---|---|
| **Can run `ALTER USER … SET RSA_PUBLIC_KEY=…` on yourself** (rare — needs OWNERSHIP on your user) | **Keypair** | Headless, works with the Dagster daemon, no browser. The script's default. |
| **Can create a PAT *and* there's already a permissive network policy attached to your user** | **PAT** | Headless. But: PATs require an associated network policy, which usually only `SECURITYADMIN` can create/attach. If your account doesn't already have one, you'll hit `Programmatic access token failed authentication. No active network policy found …` and need to fall back. |
| **None of the above** (the common case for SEs) | **SSO (externalbrowser)** | Works on any account that allows SSO. Browser tab pops once per session and the token is cached in your OS keychain via `keyring`. **Caveat:** sensors + scheduled runs (the Dagster daemon) can't open a browser, so the row-count observation sensor add-on won't fire its checks until you re-auth. For a stage demo / `dg dev` exploration this is fine. |
| **No keypair, no PAT, no SSO** | **Password** | If your account still allows it. Most don't. |

**The honest path for most SEs is SSO + sandbox mode** — read the next subsection.

### What "sandbox mode" means

The companion `setup_snowflake_environment.sh` seed script (the one that creates `DAGSTER_DEMO` with tasks / dynamic tables / procs / etc.) auto-detects when the target database **already exists and you don't own it** and **strips the DDL it can't run as your role**:

- `CREATE DATABASE` → skipped (you don't have CREATE DATABASE)
- `USE ROLE SYSADMIN` → skipped (you can't switch to roles you don't have)
- `RAW` / `STAGING` / `ANALYTICS` / `AI` schemas → renamed to `DAGSTER_DEMO_RAW` / `_STAGING` / `_ANALYTICS` / `_AI` (so the seed doesn't collide if 14 other people are also running this against the shared `SANDBOX`)
- `COMPUTE_WH` → string-substituted with whatever warehouse you actually have USAGE on

The end state is identical (tasks, DTs, procs, streams, pipes, alerts) — just scoped to schemas your role *owns* in a database you already control. Run the seed, then point the workspace setup at `SANDBOX.DAGSTER_DEMO_STAGING` and you get the full discovery story without ever needing `ACCOUNTADMIN`.

### A worked SE example

Assume your role is `SANDBOX_WRITER`, you own the `SANDBOX` database, you've got `PURINA_WAREHOUSE_USER` (or some named warehouse), and your account only allows SSO + password (no keypair registration, no PAT).

```bash
# 1. Seed: SANDBOX exists → sandbox mode auto-engages, schemas renamed
export SNOWFLAKE_TARGET_DATABASE=SANDBOX
export SNOWFLAKE_WAREHOUSE=PURINA_WAREHOUSE_USER
export SNOWFLAKE_ROLE=SANDBOX_WRITER
./setup_snowflake_environment.sh             # answer SSO, browser pops once

# 2. Workspace + every add-on (no per-prompt grinding)
export WANT_EVERYTHING=true                  # auto-y every optional add-on
./setup_snowflake_workspace_demo.sh
#   Point database = SANDBOX
#   Point schema   = DAGSTER_DEMO_STAGING
#   Auth choice    = 2 (SSO)
```

What you get: a fully scaffolded Dagster project — workspace import + `warehouse_pipeline` + `snowpark_pipeline` + Cortex + observation sensor + AutomationCondition + partitioned heterogeneous chain + freshness check + external table + dbt + 7 define-as-code DDL components — running against your SANDBOX schemas in 90 seconds, no admin help needed.

### What you **won't** be able to do as a sandboxed role

Be honest with the customer about these — they're real limits of running stage demos against a shared corporate account, not Dagster limits:

- **`ALTER TASK … RESUME`** needs `EXECUTE TASK` on the account. Most sandboxed roles don't have it, so tasks materialize as suspended. Tasks can still be run on demand from Dagster's UI (click-to-materialize calls `EXECUTE TASK <name>` which is permitted) — but they won't auto-fire on their cron schedule.
- **Materialized views** require Snowflake Enterprise edition; Standard accounts will fail the 1 MV creation in the seed. Non-blocking — the seed continues and 49/50 statements still land.
- **Snowpipe auto-ingest** needs a stage with a notification integration, which is usually `ACCOUNTADMIN`-only to create. The seed creates the pipe in `MANUAL` mode (PUT-then-COPY); auto-ingest from S3 / GCS / Azure needs your account admin to wire up the integration.

For a customer demo on a customer-owned account, none of these apply — they'll run as a powerful enough role to use everything. The above is purely about doing dry-runs on your own employer's locked-down account.

---

## Run it

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_snowflake_workspace_demo.sh -o setup_snowflake_workspace_demo.sh
chmod +x setup_snowflake_workspace_demo.sh
./setup_snowflake_workspace_demo.sh
```

Auto-installs `uv` if missing (with consent). Refuses piped invocation — it's interactive. Defaults read from `$SNOWFLAKE_ACCOUNT` / `$SNOWFLAKE_USER` / `$SNOWFLAKE_PASSWORD` / `$SNOWFLAKE_WAREHOUSE` / `$SNOWFLAKE_DATABASE` / `$SNOWFLAKE_ROLE` if you've already exported them.

### "Give me everything" mode (skip the y/N grinding)

Set `WANT_EVERYTHING=true` and every optional add-on auto-enables AND the cross-entity dep prompt is skipped (since that's the one most likely to stall on a typo). You still get the credential + auth-method + entity-type prompts — just none of the add-on selection prompts.

```bash
export WANT_EVERYTHING=true
./setup_snowflake_workspace_demo.sh
```

Individual `WANT_*` env vars still take precedence — useful for "everything except dbt":

```bash
export WANT_EVERYTHING=true
export WANT_DBT=n
./setup_snowflake_workspace_demo.sh
```

The full list of env vars (each takes `y` or `n`). **Defaults are `y` (opt-out)** for every add-on prompt — the script's purpose is showing the full Snowflake surface, so pressing Enter at each prompt gives you the full demo. Type `n` to opt out of a specific add-on. The only prompt that defaults `n` is the cross-entity dep wiring (`WANT_DEPS`) — that one's an opt-in because typing entity names is finicky.

Available env vars: `WANT_DEPS` `WANT_PIPELINE` `WANT_AUTOCOND` `WANT_CORTEX` `WANT_OBSERVER` `WANT_HET` `WANT_FRESH` `WANT_SNOWPARK` `WANT_EXTERNAL` `WANT_DBT` `WANT_DDL_SHOWCASE`.

## What it asks

1. **Project name.** Default `snowflake-dagster`. If the directory already exists, the script offers three fast paths so re-runs don't fail:
   - **[r]euse** — keeps the existing venv + installed components, *overwrites* `src/<pkg>/defs/` and `dbt/` and `.env.demo` with this run's choices. Fastest path for stage iteration ("I want to re-run with the Cortex add-on this time").
   - **[d]elete** — `rm -rf` and rebuild from scratch (incl. fresh venv + component installs).
   - **[c]hange** — pick a different project name.
2. **Credentials.** account / user / **auth method** / warehouse / database / schema / role. Verified by running `SELECT CURRENT_VERSION()` against your account before going further; offers a "continue anyway" if the verify fails (useful when you're testing scaffolding offline).

   **Auth method prompt** — pick the one your Snowflake account allows (see the *"Running this against a corporate Snowflake account"* section above for the SE-specific triage):
   - **[1] Keypair** (RSA private key file) — **the default**, and the right choice for production. Works headless (Dagster's daemon for sensors + schedules doesn't need a browser); generated `.env.demo` exports `SNOWFLAKE_PRIVATE_KEY_FILE` (+ `_PWD` if your key is encrypted); every emitted `defs.yaml` uses `authenticator: SNOWFLAKE_JWT` + `private_key_file: …`. Most enterprise accounts disable password auth — keypair is what you'll actually use **if you have OWNERSHIP on your user** (most SEs running stage demos against a corporate account don't, and need to fall back to SSO).
   - **[2] SSO** (externalbrowser) — fine for laptop `dg dev` only. A browser tab pops the first time per session; the token is cached in your OS keychain via `keyring` so subsequent `uv run` invocations don't re-prompt. **Doesn't work for the Dagster daemon** (sensors + scheduled runs can't open a browser), so if you pick this, the row-count observation sensor add-on won't be able to fire its checks until you re-auth. This is the **easiest-to-set-up choice for an SE on a locked-down corporate Snowflake**.
   - **[3] Password** — preserved for accounts that still allow it. Same `.env.demo` shape as before.
   - **[4] PAT** (Programmatic Access Token) — headless alternative to keypair when your user has OWNERSHIP isn't grantable but PATs are. **Caveat:** PATs require an attached network policy, which is `SECURITYADMIN`-only to create — if your account doesn't already have one for your user, you'll get `Programmatic access token failed authentication. No active network policy found …` and need to fall back.

   The same auth choice flows through into every generated artifact: `snowflake_workspace`, `snowflake_table_observation_sensor`, `snowpark_pipeline`, `snowflake_cortex_asset`, `dataframe_to_snowflake`, the SQLAlchemy URL for `warehouse_pipeline`, AND `dbt/profiles.yml` (when you pick the dbt add-on). One choice; consistent config everywhere.
3. **Discovery.** Queries `INFORMATION_SCHEMA.*` + `SHOW <kind>` for every entity type and prints counts:
   ```
   tasks                    12  daily_etl_orders, hourly_clickstream, monthly_revenue + 9 more
   dynamic_tables            4  customer_360, paid_orders_dt + 2 more
   stored_procedures         7  sp_seed_orders, sp_recalc + 5 more
   streams                   2  orders_stream, customers_stream
   snowpipes                 1  events_pipe
   stages                    3  …
   materialized_views        0  —
   external_tables           1  s3_logs_external
   alerts                    0  —
   ```
4. **Pick entity types.** `y/n` per type. Sensible defaults: tasks + dynamic_tables on; everything else off. Tighter scoping = less Snowflake metadata in your project and faster `dg dev` loads.
5. **Optional pattern filters.** `filter_by_name_pattern` / `exclude_name_pattern` (regex). E.g. only import names matching `HOURLY_*` and skip anything matching `_TEMP$`.
6. **Optional cross-entity deps.** Same flow as `databricks_workspace.md`: shows your imports numbered, asks "what does this depend on" for each, wires `assets_by_name` with `deps:` overrides under the hood.
7. **Optional add-ons** — each prompted separately so you can stage exactly the demo you want. Every one of these corresponds to a specific row in the *"Why Dagster?"* table above:
   - **Multi-step `warehouse_pipeline`** — pick two of your tables, get a generated multi-step asset that joins them, adds a commission column via `op: sql`, groups by region, and writes two output tables (one per sink). All compute pushed to Snowflake.
   - **`snowflake_cortex_asset`** — picks `summarize` / `sentiment` / `complete` and an input string. Cortex LLM runs server-side, no extra API key.
   - **`snowflake_table_observation_sensor`** — watches a chosen table for row-count changes (default: `RAW.ORDERS`). Materializes a downstream asset directly when changes are detected. Demonstrates the *"react to table mutation beyond what Snowpipe expresses"* pattern.
   - **`AutomationCondition.eager()` on the pipeline** — only offered if you selected the multi-step pipeline above. Wires the pipeline asset to auto-fire the moment any of its imported upstreams change. Demonstrates *"fire when upstreams finish"* declarative chaining (vs. cron + AFTER).
   - **Partitioned Python → Snowflake landing chain** — scaffolds a daily-partitioned `synthetic_data_generator` (Python) feeding `dataframe_to_snowflake`. Two claims in one: cross-engine lineage (Python on the left, Snowflake on the right) AND first-class partition replay — backfill 30 days from the `dg dev` UI with concurrency control.
   - **`freshness_check` asset check** — attaches a fail-if-not-updated-within-N-hours check to one of the imported entities. Demonstrates per-asset data quality with native pass/fail surfacing.
   - **`snowpark_pipeline` (DataFrame parallel to `warehouse_pipeline`)** — same multi-step shape (`steps` / `ref` / `op: sql` / multi-sink), but compiles to a single Snowflake SQL statement *via Snowpark's lazy DataFrame API* instead of CTE-CTAS. Including both in the same demo shows Dagster works equally well with either Snowflake compute paradigm.
   - **`external_snowflake_table`** — declare-only asset for a table managed by someone else (different team, replicated in via an external tool, etc.). Dagster's graph sees it as an upstream / sibling and reasons about lineage without taking ownership. Common enterprise pattern.
   - **Official `dagster-dbt` integration** — scaffolds a tiny dbt project under `./dbt/` (2 staging models + 1 mart, building on `RAW.ORDERS` + `RAW.CUSTOMERS`) and imports every dbt model as a Dagster asset via `DbtProjectComponent`. Lineage spans `RAW.*` (sources) → staging views → mart table, all in one graph alongside the workspace's tasks/DTs/procs.

## What gets generated

```
snowflake-dagster/
├── .env.demo                              # mode 600, gitignored, contains your password
├── pyproject.toml                         # snowflake-connector-python pinned
├── dbt/                                   # (if you picked the dbt add-on)
│   ├── dbt_project.yml
│   ├── profiles.yml                       # uses $SNOWFLAKE_* env vars
│   └── models/
│       ├── staging/{stg_orders.sql, stg_customers.sql, sources.yml}
│       └── marts/customer_revenue.sql
└── src/snowflake_dagster/
    ├── components/                        # community component sources scaffolded
    │   ├── snowflake_workspace/
    │   ├── warehouse_pipeline/             (if pipeline add-on)
    │   ├── snowflake_cortex_asset/         (if Cortex add-on)
    │   ├── snowflake_table_observation_sensor/  (if observation add-on)
    │   ├── synthetic_data_generator/        (if partitioned-heterogeneous add-on)
    │   ├── dataframe_to_snowflake/          (if partitioned-heterogeneous add-on)
    │   ├── freshness_check/                 (if freshness add-on)
    │   ├── snowpark_pipeline/               (if snowpark add-on)
    │   └── external_snowflake_table/        (if external-table add-on)
    └── defs/
        ├── snowflake_workspace/             # imports entities + assets_by_name deps
        ├── regional_top_paid_pipeline/      (optional — warehouse_pipeline)
        ├── snowpark_pipeline_demo/          (optional — snowpark_pipeline)
        ├── cortex_demo/                     (optional — Cortex)
        ├── row_count_observer/              (optional — observation sensor)
        ├── python_daily_events/             (optional — partitioned heterogeneous)
        ├── python_daily_events_to_snowflake/(optional — partitioned heterogeneous)
        ├── freshness_check_demo/            (optional — freshness)
        ├── external_table_demo/             (optional — external_snowflake_table)
        └── dbt_project/                     (optional — dbt)
```

### `defs/snowflake_workspace/defs.yaml`

```yaml
type: snowflake_dagster.components.snowflake_workspace.component.SnowflakeWorkspaceComponent
attributes:
  account:  "{{ env('SNOWFLAKE_ACCOUNT') }}"
  user:     "{{ env('SNOWFLAKE_USER') }}"
  password: "{{ env('SNOWFLAKE_PASSWORD') }}"
  warehouse: COMPUTE_WH
  database:  ANALYTICS
  schema:    STAGING
  role: TRANSFORMER
  import_tasks: true
  import_dynamic_tables: true
  import_stored_procedures: false
  # ... per-type toggles ...
  filter_by_name_pattern: "HOURLY_.*"
  assets_by_name:
    HOURLY_REVENUE_AGG:
      deps:
        - tasks/hourly_clickstream
        - dynamic_tables/customer_360
```

`assets_by_name` mirrors `DatabricksWorkspaceComponent.assets_by_task_key` exactly. Per-entity keys (all optional):

| Key | Effect |
|---|---|
| `key` | Renames the Dagster asset key (slash-separated `→ AssetKey`) |
| `group_name` | Override the group |
| `description` | Override the description |
| `deps` | List of upstream asset keys. **Merged** with whatever the component auto-discovers from Snowflake's task dependency metadata. |
| `metadata` | Dict merged into the auto-emitted metadata |
| `tags` | Dict merged into asset tags |
| `kinds` | List of kind strings (overrides inference) |
| `owners` | List of owners |

### `defs/regional_top_paid_pipeline/defs.yaml` (optional)

```yaml
type: snowflake_dagster.components.warehouse_pipeline.component.WarehousePipelineComponent
attributes:
  asset_name: regional_top_paid_pipeline
  dialect: snowflake
  database_url_env_var: SNOWFLAKE_URL
  steps:
    - id: delivered_orders
      source: {kind: table, table: RAW.ORDERS}
      operations:
        - {op: filter, predicate: "STATUS = 'delivered'"}

    - id: vip_customers
      source: {kind: table, table: RAW.CUSTOMERS}
      operations:
        - {op: filter, predicate: "LIFETIME_VALUE > 3000"}

    - id: enriched
      source: {kind: ref, ref: delivered_orders}
      operations:
        - {op: join, right: {ref: vip_customers}, on_columns: [CUSTOMER_ID], how: inner}
        - op: sql                          # ← escape hatch for anything the DSL can't model
          sql: |
            SELECT *, TOTAL * 0.15 AS COMMISSION
            FROM <<self>>
        - op: group_by
          group_by: [STATE]
          aggregations:
            REVENUE:          {col: TOTAL,      agg: sum}
            TOTAL_COMMISSION: {col: COMMISSION, agg: sum}

    - id: top_states
      source: {kind: ref, ref: enriched}
      operations:
        - {op: top_n, sort_by: REVENUE, n: 3, ascending: false}

  sinks:
    - {from: enriched,   table: ANALYTICS.STATE_ENRICHED, mode: replace}
    - {from: top_states, table: ANALYTICS.TOP_3_STATES,   mode: replace}
```

Each sink compiles to its own `CREATE OR REPLACE TABLE … AS WITH …` statement; the WITH clause carries every step's CTE so Snowflake's optimizer sees the entire graph.

## What you get in `dg dev`

```bash
cd snowflake-dagster
source .env.demo
uv run dg check defs        # validate every defs.yaml
uv run dg dev               # opens UI at http://localhost:3000
```

- **Asset graph** — your tasks, dynamic tables, stored procs, streams, etc. as nodes, with cross-entity edges from `assets_by_name.deps` (and Snowflake's own task-dependency graph, auto-discovered)
- **Click-to-materialize** — triggers the actual Snowflake task / refreshes the dynamic table / calls the stored procedure / etc. Run status streams into Dagster's timeline
- **Lineage that crosses tools** — Dagster sees: your Snowflake-side pipeline ← raw tables ← (optionally) other Dagster ingestion components landing data INTO Snowflake (via `dataframe_to_snowflake`, `dataframe_to_snowflake_bulk`, `external_snowflake_table`). One graph for the whole flow.

## Switching auth (password → PAT / key-pair / SSO)

The `snowflake_workspace` component accepts any field's `<field>_env_var` alternate AND `password`, `authenticator`, `private_key`, `token`. Edit the workspace `defs.yaml`:

```yaml
# Programmatic Access Token (newer alternative to passwords):
attributes:
  account: "{{ env('SNOWFLAKE_ACCOUNT') }}"
  user:    "{{ env('SNOWFLAKE_USER') }}"
  token:   "{{ env('SNOWFLAKE_PAT') }}"
  authenticator: "PROGRAMMATIC_ACCESS_TOKEN"

# Key-pair JWT:
attributes:
  account: "{{ env('SNOWFLAKE_ACCOUNT') }}"
  user:    "{{ env('SNOWFLAKE_USER') }}"
  private_key: "{{ env('SNOWFLAKE_PRIVATE_KEY') }}"
  authenticator: "SNOWFLAKE_JWT"

# Browser-based SSO (good for local dev):
attributes:
  account: "{{ env('SNOWFLAKE_ACCOUNT') }}"
  user:    "{{ env('SNOWFLAKE_USER') }}"
  authenticator: "externalbrowser"
```

## Layer in more Snowflake components

The community registry ships a wide Snowflake surface. Compose them as needed:

| Component | What it does |
|---|---|
| `dataframe_to_snowflake` | Write a pandas/polars DataFrame to a Snowflake table |
| `dataframe_to_snowflake_bulk` | Bulk-load (PUT + COPY INTO) for large frames |
| `external_snowflake_table` | Declare-only asset (lineage without management) |
| `snowflake_resource` | Shared connection resource for hand-written assets |
| `snowflake_io_manager` | Pandas DataFrames → table per asset, automatically |
| `snowflake_polars_io_manager` | Same, polars DataFrames |
| `snowflake_pyspark_io_manager` | Same, PySpark DataFrames |
| `snowflake_table_observation_sensor` | Watch a table for new rows / row count changes |
| `snowflake_access_history_ingestion` | Pull `ACCOUNT_USAGE.ACCESS_HISTORY` into Dagster |
| `snowflake_cortex_asset` | Call Snowflake Cortex LLMs as a Dagster asset |
| `warehouse_pipeline` | Multi-step CTE pipeline (Snowflake dialect supported — same shape as the optional add-on above) |
| `snowpark_pipeline` | Snowpark DataFrame multi-step pipeline |

Add any of them with:

```bash
uvx --from dagster-community-components-cli dagster-component add <name>
```

## Deploying to production

| Path | Commands | Docs |
|---|---|---|
| **Dagster+ Serverless** (push from laptop) | `uv add --dev dagster-cloud-cli && uv run dg plus deploy` | [Serverless quickstart](https://docs.dagster.io/dagster-plus/deployment/serverless) |
| **Dagster+ Hybrid** (CI/CD via GitHub Actions) | `uv run dagster-cloud ci init` → commit → add `DAGSTER_CLOUD_API_TOKEN` secret | [Code locations](https://docs.dagster.io/dagster-plus/deployment/code-locations) |
| **Self-hosted Dagster OSS** | Build your image; deploy as a gRPC code location | [Deployment](https://docs.dagster.io/deployment) |

**Credentials in production:**
- `.env.demo` is mode 600 + gitignored — don't commit it
- In Dagster+ UI: **Deployment → Environment variables** → add `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_PASSWORD` (or `SNOWFLAKE_PAT` if you switched to PAT)
- Use a **service-account user** with a tightly-scoped role (read on the imported entities; usage on the warehouse) — not your personal account

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Could not verify connection` | Check account format (`org-acct` or `xy12345.us-east-1`). Confirm warehouse + database + role exist and you have access. |
| Discovery prints `(skipped: …)` for a type | Your role lacks `USAGE` on the relevant `INFORMATION_SCHEMA` / `SHOW` view. Per-type skip is non-fatal — re-run with a stronger role for full coverage. |
| `dg check defs` fails on the workspace asset | Run `dg check defs --verbose` — most errors are `assets_by_name` typos. Each entity name in `assets_by_name` must match a real entity discovered at runtime. |
| Cortex asset errors `function COMPLETE does not exist` | Your account region doesn't expose Cortex yet. See [Cortex availability](https://docs.snowflake.com/en/user-guide/snowflake-cortex/llm-functions#availability). Drop the Cortex asset for now. |
| Multi-step pipeline asset fails with `<<self>>` parser error | Means you're on an older `warehouse_pipeline` (pre-chevron-syntax). Re-run setup; `dagster-component add --auto-install` pulls the latest. |

## See also

- [`databricks_workspace.md`](databricks_workspace.md) — same pattern, Databricks side
- [`warehouse_native_pipeline.md`](warehouse_native_pipeline.md) — every `warehouse_*` component, full reference, including the new multi-step `warehouse_pipeline` shape
- Per-component READMEs in the [templates repo](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets) for every Snowflake component listed above
