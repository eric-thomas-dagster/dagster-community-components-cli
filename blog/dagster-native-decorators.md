---
title: "Augment any Dagster asset with layered components"
date: 2026-09-11
author: Eric Thomas
description: "Dagster ships more than most teams use. The context.log stream, the event log, dg.RetryRequested, dynamic outputs, AssetCheckResult, and cheap event-log queries add up to a serious platform for cross-cutting behavior. Here are 17 decorators that wire those primitives together into retry classification, wall-clock SLAs, LLM cost caps, PII scrubbing, point-in-time snapshots, write-audit-publish, and more."
---

# Augment any Dagster asset with layered components

*Dagster ships more than most teams use. The `context.log` stream, the
event log, `dg.RetryRequested`, dynamic outputs, `AssetCheckResult`, and
cheap event-log queries add up to a serious platform for cross-cutting
behavior. Here are 17 decorators that wire those primitives together —
retry classification, wall-clock SLAs, LLM cost caps, PII scrubbing,
point-in-time snapshots, write-audit-publish, and more.*

**Eric Thomas · September 2026**

---

Most Dagster components create new assets. They ingest from vendors,
sync workspaces, land data in warehouses.

There's a second, quieter shape that produces nothing of its own — it
wraps another component's work and layers cross-cutting behavior around
it. Retry classification. Wall-clock SLAs. LLM cost caps. PII scrubbing.
Point-in-time snapshots. Write-audit-publish.

None of it is new capability. It's all wired out of Dagster primitives
most teams never touch — the `context.log` stream, the event log,
`dg.RetryRequested`, dynamic outputs, `AssetCheckResult`. This post
walks the 17 that shipped and, more usefully, which Dagster primitive
each one exploits.

## What the 17 do

Each one ships as a Python decorator + a YAML component, backed by one
helper module. Pick the shape that fits your team.

### Runtime control

| Component | What it does |
|---|---|
| [**`@cached`**](https://dagster-component-ui.vercel.app/c/cached_asset) | Content-addressable cache — skip compute when `code_version` + inputs haven't changed. |
| [**`@budget`**](https://dagster-component-ui.vercel.app/c/budget_asset) | Cost caps for LLM/API spend — sum `cost_usd` observations over a rolling window; short-circuit when over budget. |
| [**`@sla`**](https://dagster-component-ui.vercel.app/c/sla_asset) | Wall-clock SLAs on compute — emit `sla_breach=true` observations, escalate on repeated breach. |
| [**`@timeout`**](https://dagster-component-ui.vercel.app/c/timeout_asset) | Hard-kill runaway compute at N seconds. |
| [**`@smart_retry`**](https://dagster-component-ui.vercel.app/c/smart_retry) | Retry with classification — on `429` but not `4xx`, on `openai.RateLimitError` but not `ValueError`, per-exception-class backoff. |
| [**`@throttle`**](https://dagster-component-ui.vercel.app/c/throttle_asset) | Cross-run rate limits — read the last materialization timestamp, skip or fail if the gap is too small. |

### Safety & lifecycle

| Component | What it does |
|---|---|
| [**`@shadow`**](https://dagster-component-ui.vercel.app/c/shadow_asset) | Dual-run new vs old — run alt impl in parallel, diff outputs, flip when clean. Vendor migrations without risking prod. |
| [**`@dry_run`**](https://dagster-component-ui.vercel.app/c/dry_run_asset) | Safe-mode any asset — run compute, discard writes. Enable via YAML field, run tag, or env var. |
| [**`@snapshot`**](https://dagster-component-ui.vercel.app/c/snapshot_asset) | Point-in-time snapshots — write a `code_version`-keyed parquet after every materialization. Rollback becomes an event-log query. |
| [**`@sensitive`**](https://dagster-component-ui.vercel.app/c/sensitive_asset) | PII scrubbing in the event log — regex + field list applied before it lands. Strategies: redact / hash / mask. |
| [**`@on_hooks`**](https://dagster-component-ui.vercel.app/c/hooks_asset) | Lifecycle callbacks — on_start / on_success / on_failure / on_end. |
| [**`@lifecycle`**](https://dagster-component-ui.vercel.app/c/lifecycle_wap) (WAP) | Write-audit-publish — stage → audit → promote or quarantine. |

### Observability

| Component | What it does |
|---|---|
| [**`@profile`**](https://dagster-component-ui.vercel.app/c/profile_asset) | Auto-profile every materialization — per-column `null_ratio` / `distinct_count` / min / max / mean / std as observations. |
| [**`@log_prints`**](https://dagster-component-ui.vercel.app/c/log_prints_asset) | Route Python `print()` into Dagster logs — port legacy scripts without a rewrite. |
| [**`@data_contract`**](https://dagster-component-ui.vercel.app/c/data_contract) | Producer/consumer schema contracts — producer emits a snapshot each run; consumer refuses on incompatible upstream. |
| [**`@partition_lock`**](https://dagster-component-ui.vercel.app/c/partition_lock_asset) | Per-partition mutex — prevents concurrent runs from clobbering the same partition. State in the event log. |

### Dynamic sub-steps

| Component | What it does |
|---|---|
| [**`@task` / `@task_asset` / `TaskAssetComponent`**](https://dagster-component-ui.vercel.app/c/task_asset) | Prefect-style dynamic sub-steps — runtime-declared sub-steps under an asset with per-call durations and optional caching. |

Every one ships a runnable walkthrough that reproduces both shapes in
about sixty seconds:

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_<name>_demo.sh | bash
```

## How they work — Dagster's underused primitives

None of these decorators are magic. Every one is a small wrapper around
something Dagster already ships and most teams never touch:

- **The `context.log` stream.** Everything a compute prints or logs
  flows through here on its way to the event log. Nothing stops you
  from filtering that stream before it lands. [`@sensitive`](https://dagster-component-ui.vercel.app/c/sensitive_asset)
  redacts regex-matched PII inline. [`@log_prints`](https://dagster-component-ui.vercel.app/c/log_prints_asset)
  reroutes stray Python `print()` calls through it so legacy scripts
  get real observability without a rewrite.

- **`AssetObservation` events in the event log** are cross-run state
  you don't have to provision anything for. [`@budget`](https://dagster-component-ui.vercel.app/c/budget_asset)
  sums `cost_usd` observations over a rolling window.
  [`@throttle`](https://dagster-component-ui.vercel.app/c/throttle_asset)
  reads the last materialization timestamp.
  [`@partition_lock`](https://dagster-component-ui.vercel.app/c/partition_lock_asset)
  writes and consults a lock observation.
  [`@data_contract`](https://dagster-component-ui.vercel.app/c/data_contract)
  emits a schema snapshot each run so consumers can validate against a
  specific producer version. No Redis, no side database — just events
  queryable by any sensor.

- **`dg.RetryRequested`.** Raise it from a compute and Dagster's step
  runner re-runs the step properly — the UI goes yellow, waits,
  reruns, `context.retry_number` increments, Insights counts attempts.
  Not a hidden in-place `for` loop. [`@smart_retry`](https://dagster-component-ui.vercel.app/c/smart_retry)
  uses this to add HTTP-status and exception-class classification on
  top of Dagster's four-knob `RetryPolicy`.

- **Dynamic outputs.** Declare a fan-out at runtime, get real graph
  nodes with parallel execution and per-call durations.
  [`@task_asset`](https://dagster-component-ui.vercel.app/c/task_asset)
  builds on this to bring Prefect-style `@task` sub-steps to Dagster —
  each imperative call renders as its own graph node.

- **`AssetCheckResult`.** Attach data-quality gates to any asset that
  fail loudly and quarantine downstream automatically.
  [`@lifecycle`](https://dagster-component-ui.vercel.app/c/lifecycle_wap)
  (WAP) uses them as the audit step between staging and publish;
  failing checks block the promote and preserve the staged data for
  triage.

- **`context.instance.get_event_records()`.** Reads past events
  cheaply from any compute. Every decorator that needs history uses
  it — no in-process caches, no synced side store, no consistency
  bugs.

Dagster ships all of this. Most teams touch two or three. These
decorators are what happens when you wire the rest together on
purpose.

## Try it

Browse the full category at
👉 [dagster-component-ui.vercel.app/?category=decorator](https://dagster-component-ui.vercel.app/?category=decorator).

Every component page has the schema, the install command, and a link
to a runnable walkthrough.
