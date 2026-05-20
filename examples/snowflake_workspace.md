# Bring your existing Snowflake into Dagster — interactive setup

Run one script. Answer the prompts. Your Snowflake **tasks, dynamic tables, stored procedures, streams, pipes, stages, materialized views, external tables, and alerts** become Dagster assets — with cross-entity dependencies declared, optional multi-step SQL pipelines layered on top, and Cortex AI as a first-class asset.

## Why Dagster on top of Snowflake?

Snowflake's native scheduling (tasks + dynamic tables + alerts) is powerful **inside Snowflake**. Dagster orchestrating Snowflake earns its keep when the work the team actually does *crosses* Snowflake — into ingestion sources, Python transforms, BI tools, reverse-ETL, ML, and data-quality. The pitch in one sentence: **Snowflake schedules what runs inside Snowflake; Dagster orchestrates everything that runs anywhere and shows it as one asset graph.**

| Capability | Snowflake-only | Dagster + Snowflake |
|---|---|---|
| **Lineage across tools** | Stops at the Snowflake boundary | One graph: Fivetran / Airbyte / Sling / Python ingest → Snowflake tables → tasks/DTs → dbt → BI (Tableau/Power BI/Looker) → reverse-ETL (Hightouch/Census). Click any node, see every upstream + downstream. |
| **Heterogeneous compute** | Cross-tool work needs External Functions, custom polling, or third-party orchestrators | Snowflake + Databricks + BigQuery + Postgres + S3 + Kafka + Python tasks side-by-side in one project, with deps between them. |
| **Trigger model** | Pure cron + AFTER chains for task graphs | Cron, sensors (file landing / table changes / external events), AND `AutomationCondition` — *"materialize when this upstream changes,"* even if the upstream lives outside Snowflake. |
| **Backfills + partitions** | DIY — write a script that loops over date ranges, calls EXECUTE TASK with arguments | First-class daily/hourly/multi-dimensional partitions, parallel backfills with concurrency limits, partition mapping between assets, replay over arbitrary date ranges from the UI. |
| **Data quality** | Tasks-as-tests pattern; DIY assertions | `@asset_check` runs inline with the asset, surfaces pass/fail in the same UI, can block downstream materialization on failure. |
| **Per-asset metadata** | Task history is query-centric (duration, bytes) | Schemas auto-extracted, row counts, freshness, table preview, code-version pins, run history per-asset — all in one place. |
| **Local development** | Can't run a task outside the account | `dg dev` runs the full asset graph locally, including the bits that target a dev Snowflake account or even a DuckDB stand-in. |
| **Branching + preview deploys** | No native git-style branching for orchestration | Dagster+ branch deployments give every PR an isolated environment — preview a Snowflake change with its full downstream impact before merge. |
| **Failure surfaces** | Task failures surface in `TASK_HISTORY` view; one tool at a time | One unified timeline across all tools; a failed Snowflake task fails in Dagster with full upstream context + downstream impact, plus retry policy + alerting per-asset. |
| **Day-2 ops** | Suspend/resume per task; ALTER TASK for changes | Bulk suspend/resume, scheduled freeze windows, role-based access, audit log of who-materialized-what, alerting hooks into Slack/PagerDuty/email (in Dagster+). |

**When Dagster is overkill:** if your *entire* pipeline lives inside Snowflake and you have no upstream ingest, no Python transforms, no downstream BI orchestration, and no cross-account/cross-region work — native Snowflake tasks are fine and add no extra moving parts. The break-even point comes fast though, because most teams hit *some* of those needs within a quarter or two.

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
