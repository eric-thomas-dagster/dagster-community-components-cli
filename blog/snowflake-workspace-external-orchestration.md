---
title: "One YAML, every Snowflake object: how the community `snowflake_workspace` component turns Snowflake into a Dagster catalog"
date: 2026-09-09
author: Eric Thomas
description: A tour of the `snowflake_workspace` component — 11+ Snowflake object types as Dagster assets, every Snowflake-native event as a trigger, plus the sibling components (`snowpark_pipeline` with in-warehouse ML + Model Registry, `snowflake_cortex_agent` / `_asset` / `_search`) that turn "Snowflake is external" into a full Snowflake-native pipeline toolkit.
---

# One YAML, every Snowflake object

*A tour of the `snowflake_workspace` component — 11+ Snowflake object types as Dagster assets, every Snowflake-native event as a trigger, plus the sibling components (Snowpark pipelines, in-warehouse ML with the Snowflake Model Registry, and Cortex agents/search) that let you build a full Snowflake-native pipeline without leaving the warehouse.*

**Eric Thomas · September 2026**

---

Two customers in the last week asked me variations of the same question:

> *"Snowflake owns the storage. Snowflake owns the compute. Snowflake's
> tasks and streams and pipes and dynamic tables already do half of what
> we'd otherwise ask an orchestrator to do. Where does Dagster fit?"*

The answer, on paper, is easy: **Dagster doesn't replace any of that. It
observes and orchestrates on top.** Snowflake keeps running Snowflake
things; Dagster owns the DAG, the lineage, the schedules, the retries,
the alerts, the "did this table refresh in the last N minutes and if not
who do we page." The two systems have complementary jobs — Snowflake is
the data plane, Dagster is the control plane.

The answer *in code* has been trickier. Until recently, wiring 20 tasks
+ 15 dynamic tables + 8 Snowpipes + a Stream + an Alert into a Dagster
project was 20 + 15 + 8 + 1 + 1 = 44 hand-written `@asset` decorators.
Every new Snowflake object → another commit. Every renamed task → search
and replace. Every new customer POC → the same YAML written from scratch
with slightly different column names.

`snowflake_workspace` is what happens when you refuse to keep writing
that YAML by hand.

## The pitch, in one YAML

```yaml
type: dagster_community_components.SnowflakeWorkspaceComponent
attributes:
  workspace:
    account: {env: SNOWFLAKE_ACCOUNT}
    user: {env: SNOWFLAKE_USER}
    password: {env: SNOWFLAKE_PASSWORD}
    warehouse: COMPUTE_WH
    database: ANALYTICS
    schema: PUBLIC
    role: SYSADMIN
  import_tasks: true
  import_dynamic_tables: true
  import_snowpipes: true
  import_streams: true
  import_alerts: true
```

That's it. On the first load, the component runs `SHOW TASKS`,
`SHOW DYNAMIC TABLES`, `SHOW PIPES`, `SHOW STREAMS`, and `SHOW ALERTS`
against your Snowflake account, caches the results, and emits one
Dagster asset per object.

Materialize a task's asset in the Dagster UI → `EXECUTE TASK <name>`
runs in Snowflake, and Dagster polls to terminal state (`SUCCEEDED` /
`FAILED` / `CANCELLED`) before completing.
Materialize a Snowpipe → `ALTER PIPE <name> REFRESH`.
Materialize a dynamic table → `ALTER DYNAMIC TABLE <name> REFRESH`.
Observe an alert → `SHOW ALERTS LIKE '<name>'` + `ALERT_HISTORY`.

The Snowflake side of your stack stays entirely intact. Nothing about
Dagster requires you to move data, rebuild pipelines, or change how
tasks are scheduled inside Snowflake. **Every object stays where it is;
Dagster just gains lineage + orchestration + observability on top.**

## What it actually covers

Eleven-plus object types, all under the same `import_*` flag pattern:

| Snowflake concept | Flag | What Dagster does when you materialize |
|---|---|---|
| Task | `import_tasks` | `EXECUTE TASK`, polls `TASK_HISTORY` to terminal state, attaches query perf metadata |
| Stored procedure | `import_stored_procedures` | `CALL <proc>(args…)`, captures return value + perf |
| Dynamic table | `import_dynamic_tables` | Two modes — `external` (declare-only, refreshes reflected via sensor) or `asset` (manual `ALTER … REFRESH` from Dagster) |
| Stream | `import_streams` | **External asset** — the observation sensor probes `SYSTEM$STREAM_HAS_DATA` + `QUERY_HISTORY.rows_advanced_7d` and emits `AssetMaterialization` when CDC state advances. Tile turns green on each new consumption. |
| Snowpipe | `import_snowpipes` | `ALTER PIPE <name> REFRESH`, exposes `SYSTEM$PIPE_STATUS` as metadata |
| Stage | `import_stages` | **External asset** — sensor runs `LIST @stage`, emits materialization when file_count or total_bytes changes |
| Materialized view | `import_materialized_views` | `ALTER MATERIALIZED VIEW <name> REFRESH` |
| External table | `import_external_tables` | `ALTER EXTERNAL TABLE <name> REFRESH` |
| Alert | `import_alerts` | **External asset** — sensor runs `SHOW ALERTS` + `ALERT_HISTORY`, emits materialization on each new evaluation |
| OpenFlow flow | `import_openflow_flows` | **External asset** — sensor queries `SNOWFLAKE.TELEMETRY.EVENTS`, emits materialization when new metrics land |
| Table / view | `import_tables` / `import_views` | *Not recommended for most cases — see below.* Configurable per object as `observable` / `asset` / `virtual`. |

Multiply that across a real Snowflake account and you're looking at
50-500 Dagster assets from a component definition that fits on a phone
screen.

### A word on `import_tables` and `import_views`

I'd usually recommend **leaving these off**. Regular tables and views
aren't orchestration primitives — there's no `EXECUTE`, no
`REFRESH`, no server-side event Dagster can key off. The best we can
do is poll `INFORMATION_SCHEMA.TABLES.ROW_COUNT` every N minutes, which
is a lot of API traffic for a lineage node that mostly just sits there.

The observable-source pattern that `import_tables: true` produces is
genuinely useful, but it belongs at the **edges** of a pipeline — a
source table you don't own (an upstream landing table you're watching
for new rows) or a sink table someone else consumes (where you want the
downstream freshness check to fire). It's less useful in the middle of
a graph you already control end-to-end via tasks + dynamic tables +
Snowpipes.

If you have a specific handful of tables you *do* want as observable
sources or lineage-only virtual assets, use the single-object
`snowflake_iceberg_table` / `snowflake_time_travel_asset` /
`external_snowflake_table` components — targeted, one-per-declaration,
no bulk enumeration cost. That's the right tool for the "watch this
specific landing table" pattern.

## The questions that keep coming up

Here are the six most common asks I hear on POCs, and how the workspace
answers each.

### 1. "Can we get a freshness alert if table X hasn't had rows in N minutes?"

Yes, via Dagster's own `FreshnessPolicy` applied through the workspace's
per-asset override map:

```yaml
attributes:
  import_tables: true
  assets_by_name:
    my_analytics_table:
      freshness:
        maximum_lag_minutes: 30
        cron_schedule: "*/15 * * * *"
```

Under the hood, this is exactly the same `FreshnessPolicy` mechanism
you'd use on a hand-rolled `@asset` — the workspace just wires it in for
you at load time, and every enumerated object gets the same override
shape.

### 2. "Can Dagster monitor Snowpipe via `COPY_HISTORY` / `SYSTEM$PIPE_STATUS`?"

Yes, natively. Setting `import_snowpipes: true` produces one Dagster
asset per pipe with `SYSTEM$PIPE_STATUS(<pipe>)` polled on every
materialization and attached as metadata. Load lag, pending file count,
current-execution-state — all show up in the Dagster UI as part of the
asset's per-run metadata history, which the platform auto-plots.

### 3. "When a Snowflake task completes / fails, can that trigger a downstream Dagster job?"

Yes. Materialize a task asset in Dagster → Dagster runs `EXECUTE TASK`,
then polls `TASK_HISTORY` until it sees a terminal state. On
`SUCCEEDED`, the asset materialization event fires — and Dagster's
`AutomationCondition.eager()` on downstream assets picks it up
automatically. On `FAILED`, the asset materialization event carries
`RunFailureSensor` bait, and the polling sensor optionally re-emits
observed failures for tasks that ran on Snowflake's own schedule
outside a Dagster-initiated `EXECUTE TASK`.

### 4. "When a Stream has data, can that fire a job without custom polling?"

Yes — `import_streams: true` produces one **external asset** (`AssetSpec`)
per stream. The observation sensor probes
`SYSTEM$STREAM_HAS_DATA(<stream>)` for the point-in-time boolean, plus
`INFORMATION_SCHEMA.QUERY_HISTORY.rows_advanced_7d` for the cumulative
signal that new rows actually flowed through the stream. When either
signature moves, the sensor emits `AssetMaterialization` (with a
stable `data_version` tag for dedup) and downstream assets keyed with
`AutomationCondition.eager()` fire.

Practically: the stream tile in the Dagster UI turns **green** each
time the CDC state advances — same visual behavior as tasks and
dynamic tables. Signature-based dedup means unchanged observations
don't re-emit and don't cascade downstream.

### 5. "Dynamic Table refresh completion or refresh lag as a trigger / freshness signal?"

Yes — this is the one you get for free. Turning on
`import_dynamic_tables: true` also spins up a dedicated polling sensor
(interval configurable via `dt_refresh_sensor_interval_seconds`) that
scans `DYNAMIC_TABLE_REFRESH_HISTORY` and emits materialization events
for every completed refresh. `TARGET_LAG`-driven auto-refreshes surface
in Dagster automatically. Downstream assets keyed with
`AutomationCondition.eager()` refresh in response.

### 6. "Per-task warehouse, query tagging, suspend/resume?"

All exposed through per-asset overrides:

```yaml
assets_by_name:
  nightly_rebuild:
    warehouse: BIG_WH
    query_tag: dagster:owner=analytics
    session_parameters:
      STATEMENT_TIMEOUT_IN_SECONDS: 3600
```

The workspace threads these through the underlying `SnowflakeResource`,
which supports every session parameter and warehouse-routing option
`dagster-snowflake` already exposes.

## This is a catalog. You still have to wire lineage across the seam.

Everything above sells the workspace as *observation + orchestration*. That's
half-true. The workspace is really a **catalog** — it makes every Snowflake
object visible in Dagster and gives you a way to trigger the ones that
have a native `RUN` / `REFRESH`. The interesting orchestration, the reason
customers care about running Dagster at all, is at the **seams**:

- A dlt job on AWS lands 3 million rows in S3 → I want Snowpipe to
  pick them up → I want a Snowflake task to run against the loaded data
  → then I want a Databricks job to consume the task's output.
- Or the reverse: a Snowflake task refreshes → I want to fire a
  webhook, kick off a dbt Cloud job, publish to Kafka, retrain an
  MLflow model, whatever.

The workspace itself does none of that. It gives you the assets; you
still have to declare the **cross-boundary lineage** and the
**automation condition** that fires when an upstream materializes.

Two mechanisms make this work in a workspace-based project.

### 1. Per-asset `deps:` overrides

Every enumerated Snowflake object accepts per-asset customization via
`assets_by_name` — including `deps:` that point at assets *outside* the
workspace. So a Snowflake task can declare it depends on a dlt-ingested
S3 asset:

```yaml
type: dagster_community_components.SnowflakeWorkspaceComponent
attributes:
  workspace:
    account: {env: SNOWFLAKE_ACCOUNT}
    # …
  import_tasks: true
  assets_by_name:
    DG_DAILY_ORDERS_ROLLUP:
      # This task's Dagster asset now shows the dlt asset as an
      # upstream — lineage graph, Dagster+ Insights, launchpad
      # backfills, everything treats them as one DAG.
      deps:
        - raw_events_dlt_ingest
      automation_condition: "{{ dg.AutomationCondition.eager() }}"
```

The `automation_condition` is the part that actually *fires* the task
when the dlt ingest materializes. `eager()` is the simplest: run this
asset the moment any upstream materializes. Read the
`AutomationCondition` docs for the more surgical options
(`on_missing`, `on_cron`, `any_deps_updated_within`, etc.).

### 2. Downstream assets that depend on the workspace's Snowflake assets

The reverse direction — Snowflake task completes → fire a Databricks
job — works the same way, just from the other side. The Databricks
asset declares the workspace-enumerated Snowflake task as its upstream:

```yaml
type: dagster_databricks.DatabricksTaskComponent
attributes:
  task_key: refresh_reporting_layer
  cluster: {existing_cluster_id: "…"}
  deps:
    # This is a Dagster asset key. The workspace enumerates the
    # Snowflake task as `snowflake/dagster_demo/staging/DG_DAILY_ORDERS_ROLLUP`
    # — grab that key from the Dagster UI and paste it here.
    - "snowflake/dagster_demo/staging/DG_DAILY_ORDERS_ROLLUP"
  automation_condition: "{{ dg.AutomationCondition.eager() }}"
```

When the Snowflake task finishes (whether Dagster ran it via
`EXECUTE TASK` or Snowflake's own schedule fired it and the workspace's
sensor observed the completion), the Databricks task fires
automatically.

### Why this isn't the workspace's job

You could imagine a version of `SnowflakeWorkspaceComponent` that
somehow auto-wires cross-boundary lineage — inspect Snowflake's
`OBJECT_DEPENDENCIES` view, scan your other components for matching
external asset keys, and stitch. It'd be tempting. It'd also be wrong.
The cross-boundary story is *your project's* — which dlt job feeds
which Snowflake table, which Databricks cluster runs which downstream
transform, when a task refresh should cascade eagerly vs. wait for a
cron — that's business logic, not catalog metadata. The workspace's
job is to surface the Snowflake side reliably; declaring the seams is
on you.

The upside: you write the wiring **once, in YAML**, using the same
`deps:` + `automation_condition` mechanism you'd use for any Dagster
asset. No custom sensors. No polling loops. No boilerplate.

### The full runnable example

The
[**`examples/snowflake_workspace.md`**](https://dagster-component-ui.vercel.app/examples/snowflake_workspace)
walkthrough is the reference implementation — a single bootstrap
(`seed.sh` + `bootstrap.sh`) provisions a complete Snowflake data
platform + Dagster project where every Snowflake primitive appears in
one connected lineage graph: RAW → dynamic tables → tasks → marts,
with `automation_condition_applicator` running `eager` on
tasks/procs/MVs/snowpipes/Iceberg and `on_cron` for root tasks.
About 30 assets, all cross-wired, deployable to Dagster+ Serverless
via [`deploy_to_dagster_plus.sh`](https://github.com/eric-thomas-dagster/dagster-community-components-cli/blob/main/examples/deploy_to_dagster_plus.sh)
as-is. Read it if you want a working project to copy the
`assets_by_name.<task>.deps + automation_condition` shape from.

## The `translation:` hook — for when you need more

Sometimes the default asset-key shape (`db.schema.object_name`) or the
default kind set (`snowflake`, `task`) isn't what you want. That's where
the `translation:` callable comes in:

```yaml
attributes:
  translation: |
    lambda base_spec, props: base_spec.replace_attributes(
      tags={
        **base_spec.tags,
        "team": "analytics" if props.database == "ANALYTICS" else "platform",
      },
      metadata={
        **base_spec.metadata,
        "cost_center": "eng-data" if props.object_kind == "task" else "eng-platform",
      },
    )
```

Every imported object — task, procedure, dynamic table, stream, Snowpipe,
stage, materialized view, external table, alert, OpenFlow flow, table,
or view — flows through this callable. Same mechanism as
`FivetranAccountComponent` / `PowerBIWorkspaceComponent`, so if you've
built one, you already know the pattern.

## Auth

Because the `workspace:` block IS a `dagster_snowflake.SnowflakeResource`,
you inherit every auth mode the official resource supports without the
workspace component knowing anything about it:

- **Password** — `password: {env: ...}`
- **Key pair** — `private_key_path`, `private_key_password`
- **SSO / external browser** — `authenticator: externalbrowser`
- **OAuth** — `authenticator: oauth` + `token`
- **JWT** — `authenticator: snowflake_jwt`
- **MFA** — password + `passcode` / `passcode_in_password`

When Snowflake adds a new auth mode upstream, the workspace lights it up
automatically. That's the whole reason it's built as a Resource wrapper
rather than as its own auth surface.

## Snowflake stays external. That's the point.

Nothing about `snowflake_workspace` moves your data. Nothing pushes
compute into Dagster. Nothing rewrites your task DAG or your dynamic
table graph or your Snowpipe integrations. The database, the warehouses,
the security posture, the FinOps — all unchanged.

What you get is the *observation and orchestration layer* on top:

- A live catalog of every Snowflake object as a Dagster asset.
- Lineage that spans Snowflake objects + external assets (S3 buckets,
  Kafka topics, dbt models, Fivetran connectors, ML models, whatever
  else lives in your Dagster project).
- Materialization from the Dagster UI that runs the actual Snowflake
  primitive (`EXECUTE TASK`, `REFRESH PIPE`, `ALTER DT REFRESH`).
- Automatic freshness alerts, per-run metadata history, failure
  routing, retries — Dagster's whole platform layer applied to
  Snowflake objects with zero glue code.
- One YAML declaration → the whole graph.

## Beyond the workspace — the Snowflake-native toolkit

The workspace is the catalog. It gives you every Snowflake object as a
Dagster asset. But the DCC registry ships **~30 Snowflake-related
components** total, and the interesting ones are the *transform* and
*inference* layers that let you build a pipeline where **the compute
stays in Snowflake even when the pipeline stages don't map to a
pre-existing Snowflake object**.

Three families worth calling out:

### `snowpark_pipeline` — multi-step Snowpark DataFrame chain, one asset

Every op builds a lazy Snowpark plan; the whole pipeline compiles to
**one** SQL statement that runs entirely in the Snowflake warehouse.
No data through Python. Reach for it when the pipeline is more complex
than a single-query `warehouse_summarize` but everything still lives in
Snowflake.

```yaml
type: dagster_community_components.SnowparkPipelineComponent
attributes:
  asset_name: gold_customers_by_region
  connection: {account_env_var: SNOWFLAKE_ACCOUNT, ...}
  steps:
    - id: paid_orders
      source: {kind: table, table: RAW.ORDERS}
      operations:
        - {op: filter, predicate: "STATUS = 'paid'"}
    - id: enriched
      source: {kind: ref, ref: paid_orders}
      operations:
        - {op: join, right: {table: RAW.CUSTOMERS}, on_columns: [CUSTOMER_ID]}
        - {op: group_by, group_by: [REGION],
           aggregations: {REVENUE: {col: AMOUNT, agg: sum}}}
  sinks:
    - {from: enriched, kind: table, table: ANALYTICS.GOLD_BY_REGION, mode: overwrite}
```

**The `ml` op — in-warehouse machine learning.**
Turn any step into a KMeans / RandomForest / XGBoost fit via
`snowflake-ml-python`. Feature prep, fit, and post-processing all
run inside the warehouse as part of the same compiled plan.

```yaml
- op: ml
  algorithm: xgb_classifier
  input_columns: [TENURE_DAYS, MONTHLY_SPEND, SUPPORT_TICKETS]
  label_columns: [CHURNED]
  output_column: PREDICTION
  mode: fit_predict
  hyperparameters: {n_estimators: 200, max_depth: 6}
```

**Snowflake Model Registry — train once, predict often.**
Add `model_name:` to any fit-mode op and the fitted estimator persists
to the Snowflake Model Registry as a timestamped version. A separate pipeline (different schedule) uses
`mode: predict` + `model_name:` to load the versioned model and score
new data — no retraining, no rebuild.

```yaml
# Training pipeline (weekly cron)
- op: ml
  mode: fit
  algorithm: xgb_classifier
  input_columns: [...]
  label_columns: [CHURNED]
  model_name: customer_churn        # ← persists to Registry

# Inference pipeline (hourly cron, separate YAML)
- op: ml
  mode: predict
  input_columns: [...]
  model_name: customer_churn        # ← loads latest version from Registry
  # model_version: latest           # default; also 'staging' | 'production' | pinned literal
```

Model versions live inside Snowflake — same RBAC, audit log, and
replication as your training data. No MLflow server to run, no S3
bucket to secure, no serializer choices to make. The `ml_pipeline`
component (sklearn / XGBoost / LightGBM outside the warehouse) ships
the same `register_model` / `load_model` shape with a `backend:
mlflow | snowflake` selector, so you can move a model between systems
without rewriting the pipeline.

### Cortex — LLM inference and vector search, native in Snowflake

Three components wrap Snowflake Cortex so agentic + RAG workloads stay
inside the warehouse:

| Component | What it does |
|---|---|
| **`snowflake_cortex_asset`** | Batch `SNOWFLAKE.CORTEX.COMPLETE(...)` over a table column. Materialize daily, land LLM completions as a new column, use Cortex-native models (`claude-3-5-sonnet`, `llama3.1-70b`, `mistral-large2`, `snowflake-arctic`, etc.) without leaving Snowflake. |
| **`snowflake_cortex_search`** | Vector search over a Cortex Search Service (Snowflake's managed vector index). Query with a top-K search string, land the ranked hits as a Dagster asset. |
| **`snowflake_cortex_agent`** | Single-shot LLM agent that speaks the same MCP-tool + typed-output shape as `openai_agent` / `anthropic_agent` / `gemini_agent` — but the model runs inside Snowflake via Cortex. Partition-aware, freshness-policy aware, `RetryPolicy`-aware. |

Same catalog-plus-transform-plus-inference story as the rest of the
Snowflake toolkit: **the workspace enumerates what's there, Snowpark
pipelines do the multi-step transforms, Cortex components run the
LLM / retrieval calls — all as first-class Dagster assets with
Snowflake as the sole runtime.**

### Snowpipe — continuous ingestion, driven from Dagster or observed

The workspace covers Snowpipes via `import_snowpipes: true`
(`ALTER PIPE <name> REFRESH` + `SYSTEM$PIPE_STATUS` metadata). For
finer control, two sibling components:

- **`snowflake_snowpipe`** — declare a single pipe with its full
  `COPY INTO` shape (target table + stage + file_format + on_error).
  Materialize triggers a refresh; per-run metadata surfaces the load
  latency + file count.
- **`snowflake_snowpipe_load_sensor`** — a standalone sensor that
  watches `INFORMATION_SCHEMA.COPY_HISTORY` for a specific pipe (or
  set of pipes) and emits materialization events for downstream
  eager cascade. Use this when a Snowpipe is driven by S3 event
  notifications (not by Dagster) and you still want the load event
  in the Dagster graph.

### Single-object components — for finer control than bulk enumeration

The workspace is the right tool when you want a whole account's worth
of tasks / DTs / streams / pipes surfaced automatically. When you want
**one specific table** as a lineage node, or **one specific task** with
custom automation, reach for the single-object component instead:

| Component | Use when |
|---|---|
| `snowflake_task` | You want one specific task as an asset with its own YAML file |
| `snowflake_dynamic_table` | Single-DT declaration, per-DT YAML for reviewability |
| `snowflake_iceberg_table` | Snowflake-managed Iceberg tables with time-travel + optimize |
| `snowflake_stream` | Single-stream declaration (targeted CDC observer) |
| `snowflake_alert` | Single-alert declaration + condition + action YAML |
| `external_snowflake_table` | Declare-only external asset (pure lineage node, no compute) — Snowflake stays external, Dagster gets it in the graph |
| `snowflake_time_travel_asset` | Point-in-time snapshot of a table (AT / BEFORE timestamp) |
| `snowflake_materialized_view` | Single MV with refresh + cluster_by + behind_by metadata |
| `snowflake_stored_procedure` | Single procedure declaration (name + args + return_type) |
| `dataframe_to_snowflake` / `dataframe_to_snowflake_bulk` | Sink assets — land a DataFrame in Snowflake (row-by-row vs `PUT + COPY INTO`) |

Rule of thumb: **workspace when you want the whole graph; single-object
components when you want per-object review, custom automation, or
non-default per-asset config.**

### The whole toolkit in one graph

Nothing forces a project to use just one of these. A common shape:

- `snowflake_workspace` — enumerates the 200 pre-existing tasks / DTs
  / streams / pipes → all show up as assets, all wired to the
  observation sensor.
- 5-10 `snowpark_pipeline` assets — the transforms that are complex
  enough to deserve their own YAML file (the multi-step joins +
  aggregates that would be too much for the workspace's auto-mapped
  tasks).
- 2-3 `snowflake_cortex_asset` assets — the LLM-batch steps.
- 1 `snowflake_cortex_agent` asset — the agentic step (with MCP tools
  for grounding).
- 1 `snowpark_pipeline` with `op: ml` (`mode: fit` + `model_name:
  customer_churn`) on a weekly cron — the trainer.
- 1 `snowpark_pipeline` with `op: ml` (`mode: predict` +
  `model_name: customer_churn`) on an hourly cron — the scorer.

Every asset above runs its compute inside the Snowflake warehouse.
Dagster owns the DAG, the lineage across all of them, the retries,
the alerts, and the "did this asset materialize in the last N
minutes" freshness story. **The data plane stayed in Snowflake; the
control plane is one YAML directory.**

## Build it, extend it, fork it

The Snowflake components live in the
[dagster-community-components][repo] registry — install with the
`dagster-component` CLI, or read the source and adapt. The registry is
where I ship the long tail of what teams actually run against Snowflake
day-to-day: streams and stages and OpenFlow flows and the Cortex trio
and multi-step Snowpark ML pipelines with Model Registry. Dagster keeps
shipping the core platform + the flagship integrations; the community
registry is where the long tail lives.

If a component here almost does what you need but not quite, **the
right move is often to fork it into your project**. The whole thing
is a few Python files and a schema — a `snowflake_workspace` variant
that filters tasks by `assets_by_name` prefix, or a `snowpark_pipeline`
variant with a domain-specific op, is an afternoon of work, not a
subclass tower. And if what you build is generally useful, PRs to the
registry are welcome.

If your team runs Snowflake and you're figuring out where Dagster fits,
this is what "fits" looks like: **one YAML declaration, every Snowflake
object as a Dagster asset, every Snowflake-native event as a Dagster
trigger, and Snowflake owns exactly what Snowflake should own.**

[repo]: https://github.com/eric-thomas-dagster/dagster-component-templates

---

**Reference:**
- Workspace component: [`snowflake_workspace`](https://dagster-component-ui.vercel.app/c/snowflake_workspace)
- Walkthrough demo: [`examples/snowflake_workspace`](https://dagster-component-ui.vercel.app/examples/snowflake_workspace)
- Transform: [`snowpark_pipeline`](https://dagster-component-ui.vercel.app/c/snowpark_pipeline) — multi-step Snowpark chain with the `ml` op + Snowflake Model Registry
- ML across pipelines: [`ml_pipeline`](https://dagster-component-ui.vercel.app/c/ml_pipeline) — sklearn / xgboost / lightgbm with MLflow or Snowflake Model Registry backend
- Cortex: [`snowflake_cortex_asset`](https://dagster-component-ui.vercel.app/c/snowflake_cortex_asset) · [`snowflake_cortex_search`](https://dagster-component-ui.vercel.app/c/snowflake_cortex_search) · [`snowflake_cortex_agent`](https://dagster-component-ui.vercel.app/c/snowflake_cortex_agent)
- Vendor page (all ~30 Snowflake components): [Snowflake](https://dagster-component-ui.vercel.app/vendors/snowflake)
