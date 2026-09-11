---
title: "Augment any Dagster asset with layered components"
date: 2026-09-11
author: Eric Thomas
description: "17 new community components layer cross-cutting behavior — retry classification, SLAs, cost caps, PII scrubbing, snapshots, WAP, and more — around any existing Dagster asset. All composable in YAML via `wraps:`, so you can stack them arbitrarily deep with zero Python."
---

# Augment any Dagster asset with layered components

*17 new community components layer cross-cutting behavior — retry
classification, SLAs, cost caps, PII scrubbing, snapshots, WAP, and
more — around any existing Dagster asset. All composable in YAML via
`wraps:` — stack them arbitrarily deep with zero Python.*

**Eric Thomas · September 2026**

---

Most Dagster components create new assets. They ingest from vendors,
sync workspaces, land data in warehouses. That's the majority of what's
in every registry.

There's a second, quieter shape that produces nothing of its own — it
wraps another component's work and layers cross-cutting behavior around
it. Retry classification. Wall-clock SLAs. LLM cost caps. PII scrubbing.
Point-in-time snapshots. Write-audit-publish.

Seventeen of them landed in the community registry this month, and they
all stack together via one YAML field (`wraps:`). This post covers what
they do, the four properties that make the pattern durable, and how to
install them.

## What the 17 do

All 17 live in the [`decorator`
category](https://dagster-component-ui.vercel.app/?category=decorator).
Each one ships as both a Python decorator and a `wraps:`-composable YAML
component (same behavior, one helper module — pick the shape that fits
your team).

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

## Stack them in YAML

The interesting part is what happens when you layer them. Every one
accepts a `wraps:` field pointing at another component — outer wraps
inner wraps inner, arbitrarily deep, zero customer Python:

```yaml
type: dagster_community_components.SnapshotAssetComponent
attributes:
  uri: s3://backups/orders                             # ← historical audit trail
  wraps:
    type: dagster_community_components.BudgetAssetComponent
    attributes:
      max_cost_per_day_usd: 5.00                       # ← daily API cost cap
      wraps:
        type: dagster_community_components.SensitiveAssetComponent
        attributes:
          scrub_fields: [email, phone, ssn]            # ← PII scrubbing
          wraps:
            type: dagster_community_components.RestApiFetcherComponent
            attributes:
              url: "https://api.example.com/orders"   # ← the actual work
              output_asset_key: orders
```

One asset registered (`orders`). Snapshotted, cost-capped, PII-scrubbed,
and ingested — every layer plugs into the next via a YAML block. Any
community component + any internal `my_company.*` component with the
same `compute:` + `wraps:` shape plugs into the same slot; the stack
doesn't care where the layers came from.

The Python analog is `@snapshot @budget @sensitive def fetch(...)`.

## Why the pattern lasts

Four properties keep this shape working long-term:

**1. Discoverability.** They live in the catalog. `dg list components`
finds them, the docs indexes them, `dagster-component add <id>` installs
them. No git-cloning some team-local `utils/decorators.py`.

**2. YAML + Python parity.** Every one ships both a Python decorator
and a YAML component, backed by one helper module. No lock-in to either
style.

**3. Event log as state store.** Cross-run state (cost history,
throttle timestamps, contract snapshots, breach counts) lands as
`AssetObservation` events — restart-safe, worker-safe, sensor-queryable,
zero new infra. No Redis, no side database.

**4. `wraps:` composability.** As shown above — any of these stack over
any other component with a single YAML field.

## Try it

Browse all 17 in the registry UI:
👉 [dagster-component-ui.vercel.app/?category=decorator](https://dagster-component-ui.vercel.app/?category=decorator)

Or via the CLI:

```bash
dagster-component search "" --category decorator
```

Install one and run its walkthrough:

```bash
dagster-component add snapshot_asset --auto-install
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_snapshot_asset_demo.sh | bash
```
