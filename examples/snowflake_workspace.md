# Bring your existing Snowflake into Dagster — interactive setup

Run one script. Answer the prompts. Your Snowflake **tasks, dynamic tables, stored procedures, streams, pipes, stages, materialized views, external tables, and alerts** become Dagster assets — with cross-entity dependencies declared, optional multi-step SQL pipelines layered on top, and Cortex AI as a first-class asset.

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

### If you're a hybrid stack (most teams)

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

Teardown with [`teardown_snowflake_environment.sql`](teardown_snowflake_environment.sql) — drops the whole `DAGSTER_DEMO` database. (OpenFlow flows can't be created via SQL DDL — they're built in the Openflow UI / Apache NiFi canvas. If you already have flows in your account, the workspace component discovers them via the `import_openflow_flows: true` flag.)

## Run it

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_snowflake_workspace_demo.sh -o setup_snowflake_workspace_demo.sh
chmod +x setup_snowflake_workspace_demo.sh
./setup_snowflake_workspace_demo.sh
```

Auto-installs `uv` if missing (with consent). Refuses piped invocation — it's interactive. Defaults read from `$SNOWFLAKE_ACCOUNT` / `$SNOWFLAKE_USER` / `$SNOWFLAKE_PASSWORD` / `$SNOWFLAKE_WAREHOUSE` / `$SNOWFLAKE_DATABASE` / `$SNOWFLAKE_ROLE` if you've already exported them.

## What it asks

1. **Project name.** Default `snowflake-dagster`. Collision-checked.
2. **Credentials.** account / user / password (hidden) / warehouse / database / schema / role. Verified by running `SELECT CURRENT_VERSION()` against your account before going further; offers a "continue anyway" if the verify fails (useful when you're testing scaffolding offline).
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
7. **Optional add-ons:**
   - **Multi-step `warehouse_pipeline`** — pick two of your tables, get a generated multi-step asset that joins them, adds a commission column via `op: sql`, groups by region, and writes two output tables (one per sink). All compute pushed to Snowflake.
   - **`snowflake_cortex_asset`** — picks `summarize` / `sentiment` / `complete` and an input string. Cortex LLM runs server-side, no extra API key.

## What gets generated

```
snowflake-dagster/
├── .env.demo                              # mode 600, gitignored, contains your password
├── pyproject.toml                         # snowflake-connector-python pinned
└── src/snowflake_dagster/
    ├── components/                        # community component sources scaffolded
    │   ├── snowflake_workspace/
    │   ├── warehouse_pipeline/             (if you picked the pipeline add-on)
    │   └── snowflake_cortex_asset/         (if you picked the Cortex add-on)
    └── defs/
        ├── snowflake_workspace/
        │   └── defs.yaml                  # imports your entities, plus assets_by_name deps
        ├── regional_top_paid_pipeline/    (optional add-on)
        │   └── defs.yaml
        └── cortex_demo/                   (optional add-on)
            └── defs.yaml
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
