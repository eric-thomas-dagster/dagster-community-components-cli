---
title: "One YAML, every Snowflake object: how the community `snowflake_workspace` component turns Snowflake into a Dagster catalog"
date: 2026-08-17
author: Eric Thomas
description: A tour of the `snowflake_workspace` component — 11+ Snowflake object types as Dagster assets, every Snowflake-native event as a trigger, and Snowflake stays exactly where it is.
---

# One YAML, every Snowflake object

*A tour of the `snowflake_workspace` component — 11+ Snowflake object types as Dagster assets, every Snowflake-native event as a trigger, and Snowflake stays exactly where it is.*

**Eric Thomas · August 2026**

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
| Stream | `import_streams` | Observable — polls `SYSTEM$STREAM_HAS_DATA` + `STREAM_HAS_DATA_SINCE` |
| Snowpipe | `import_snowpipes` | `ALTER PIPE <name> REFRESH`, exposes `SYSTEM$PIPE_STATUS` as metadata |
| Stage | `import_stages` | Observable — polls stage file listings |
| Materialized view | `import_materialized_views` | `ALTER MATERIALIZED VIEW <name> REFRESH` |
| External table | `import_external_tables` | `ALTER EXTERNAL TABLE <name> REFRESH` |
| Alert | `import_alerts` | Observable — polls `SHOW ALERTS` state + `ALERT_HISTORY` |
| Openflow flow | `import_openflow_flows` | Observable — reads flow telemetry from `SNOWFLAKE.TELEMETRY.EVENTS` |
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

Yes — `import_streams: true` produces one observable source asset per
stream. Its observation function polls
`SYSTEM$STREAM_HAS_DATA(<stream>)`; when the boolean flips true, the
`DataVersion` changes, and Dagster's declarative automation treats it
like any other data-version change on an upstream asset.

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

## What's next

The community `snowflake_workspace` component ships in the
[dagster-community-components][repo] registry today, and I'm proposing
to promote it directly into the official `dagster-snowflake` package.
The syntax matches `DatabricksWorkspaceComponent` /
`FivetranAccountComponent` / `PowerBIWorkspaceComponent` line-for-line —
same `workspace:` top-level field, same `@public` API, same
`StateBackedComponent` internal shape, same `translation:` hook. Nothing
new for reviewers to learn.

If your team runs Snowflake and you're figuring out where Dagster fits,
this is what "fits" looks like: **one YAML declaration, every Snowflake
object as a Dagster asset, every Snowflake-native event as a Dagster
trigger, and Snowflake owns exactly what Snowflake should own.**

[repo]: https://github.com/eric-thomas-dagster/dagster-component-templates

---

**Reference:**
- Component source: [`integrations/snowflake_workspace/component.py`][src]
- Walkthrough demo: [`examples/snowflake_workspace.md`][demo]
- Companion object components (single-DT, single-task, single-pipe, etc.):
  [`integrations/snowflake_*/`][companions]

[src]: https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/integrations/snowflake_workspace/component.py
[demo]: https://github.com/eric-thomas-dagster/dagster-community-components-cli/blob/main/examples/snowflake_workspace.md
[companions]: https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/integrations
