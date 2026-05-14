# Composition primitives — small jobs without external auth

Dagster pipelines aren't only assets. Most production projects need a handful of small **jobs**: nightly database VACUUM, uptime heartbeats, calls to internal services, scheduled callbacks into Python utilities. The community registry has a family of `*_job` components that wrap each pattern declaratively — pure YAML, no Python.

This walkthrough sets up five of them in one project, with **no SaaS / cloud / auth** required: SQLite covers SQL, `httpbin.org` covers HTTP, an in-package Python function covers the callable case.

## Components used

| Component | Source | Role |
|---|---|---|
| `python_callable_job` | community | Wraps any `module.path:function` as an op job |
| `http_webhook_job` | community | Fires one HTTP request as an op job |
| `observability_heartbeat_job` | community | Multi-target heartbeat (HTTP / Slack / Teams / PagerDuty) |
| `warehouse_maintenance_job` | community | N SQL statements sequentially against SQLAlchemy URL |
| `sql_command_job` | community | One multi-statement SQL block against SQLAlchemy URL |

All five inherit the same shape:

```yaml
job_name: <unique>
schedule: "<cron, optional>"
default_status: STOPPED | RUNNING
tags: { ... }
```

…plus per-component fields (`callable_path`, `url`, `statements`, etc.).

## Run it

```bash
bash setup_composition_primitives_demo.sh
cd composition-primitives-demo
export WAREHOUSE_URL='sqlite:////tmp/composition-primitives-demo.db'

uv run dg check defs
uv run dg list defs
```

Five jobs, no assets.

## Launch each

```bash
uv run dg launch --job nightly_cleanup               # python_callable_job
uv run dg launch --job ping_status_endpoint          # http_webhook_job
uv run dg launch --job platform_heartbeat            # observability_heartbeat_job
uv run dg launch --job warehouse_nightly_maintenance # warehouse_maintenance_job
uv run dg launch --job refresh_summaries             # sql_command_job
```

Expect `RUN_SUCCESS` on each.

## What each one does

### `python_callable_job`

```yaml
type: dagster_component_templates.PythonCallableJobComponent
attributes:
  job_name: nightly_cleanup
  callable_path: composition_primitives_demo.tasks:cleanup
  kwargs: { days: 30 }
```

Resolves `module.path:function` at job execution time and calls it with the YAML `kwargs`. Return value is recorded as op output. Right for: cron-driven cleanups, simple Python tasks that don't justify a full asset.

### `http_webhook_job`

```yaml
type: dagster_component_templates.HttpWebhookJobComponent
attributes:
  job_name: ping_status_endpoint
  url: https://httpbin.org/status/200
  method: GET
  expected_status: 200
```

Fires one HTTP request, fails the op unless `expected_status` matches. Right for: pinging internal services, kicking SaaS triggers, simple URL probes that aren't full observation sensors.

### `observability_heartbeat_job`

```yaml
type: dagster_component_templates.ObservabilityHeartbeatJobComponent
attributes:
  job_name: platform_heartbeat
  message: "data platform heartbeat"
  http_heartbeat_url: https://httpbin.org/get      # UptimeRobot / BetterUptime / etc.
  # Optional outputs — turn on any combination:
  # slack_webhook_url_env: SLACK_OPS_WEBHOOK
  # pagerduty_routing_key_env: PD_ROUTING_KEY
  # teams_webhook_url_env: TEAMS_WEBHOOK
```

Multi-target heartbeat. Any of the four sinks can be wired or left unset — only the configured ones fire. Right for: liveness signals to UptimeRobot/PagerDuty/Slack on a cron, without writing the boilerplate four times.

### `warehouse_maintenance_job`

```yaml
type: dagster_component_templates.WarehouseMaintenanceJobComponent
attributes:
  job_name: warehouse_nightly_maintenance
  connection_string_env: WAREHOUSE_URL   # SQLAlchemy URL — any backend
  statements:
    - ANALYZE orders
    - ANALYZE customers
    - SELECT COUNT(*) FROM orders
  autocommit: true
  fail_fast: false
```

Each statement runs as its own op-level call. `fail_fast: false` collects errors and continues; `fail_fast: true` halts on first failure. Right for: nightly `VACUUM ANALYZE`, refreshing materialized views, periodic table stats updates. In this demo the URL points at SQLite — substitute Postgres / Snowflake / Redshift to retarget.

### `sql_command_job`

```yaml
type: dagster_component_templates.SqlCommandJobComponent
attributes:
  job_name: refresh_summaries
  connection_string_env: WAREHOUSE_URL
  sql: |
    CREATE TABLE IF NOT EXISTS daily_revenue (day TEXT, total REAL);
    DELETE FROM daily_revenue;
    INSERT INTO daily_revenue SELECT date('now'), SUM(total) FROM orders;
```

Same engine as `warehouse_maintenance_job`, but takes a single multi-statement SQL block (semicolon-separated) instead of a list. Right for: small ad-hoc procedures that read like one SQL script.

## Trade-offs

- **op-job vs asset.** These five all produce op jobs (no assets in the graph). They show up in the Jobs tab and can be launched on cron or on demand. If you want lineage tracking, use `dataframe_to_*` sinks or `*_ingestion` assets instead.
- **Schedules default to `STOPPED`.** Switch to `RUNNING` in YAML (or via the UI) once you've sanity-checked the cadence.
- **All five accept `schedule:` and `tags:`.** Cron schedules built into the component, no separate scaffold needed.

## See also

- [`external_assets.md`](external_assets.md) — declare-only side of the registry
- [`automation_condition_pipeline.md`](automation_condition_pipeline.md) — declarative automation for asset-side jobs
