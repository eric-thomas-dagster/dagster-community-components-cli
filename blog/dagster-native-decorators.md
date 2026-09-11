---
title: "Build your own Dagster decorators — as components"
date: 2026-09-11
author: Eric Thomas
description: "Dagster has an under-explored extension shape: a component that produces nothing of its own, only wraps another component's work with cross-cutting behavior. It ships with a schema, a catalog entry, a Python decorator counterpart, event-log-backed state, and — via one YAML field, `wraps:` — arbitrarily deep composability. This post is the pattern, the four properties that make it scale, a walkthrough of building one, and the seventeen examples I shipped as starting points."
---

# Build your own Dagster decorators — as components

*Dagster has an under-explored extension shape: a component that produces
nothing of its own, only wraps another component's work with cross-cutting
behavior. It ships with a schema, a catalog entry, a Python decorator
counterpart, event-log-backed state, and — via one YAML field, `wraps:` —
arbitrarily deep composability. This post is the pattern, the four
properties that make it scale, and the seventeen examples I shipped as
starting points.*

**Eric Thomas · September 2026**

---

Dagster has a lot of extension points. Assets. Sensors. Resources. IO
managers. Custom types. Components.

There's another one that doesn't quite feel like an extension point until
you've used it a few times: **a component whose entire job is to wrap
another component's work.** It doesn't produce any assets of its own. It
takes someone else's compute — a REST fetch, a SQL query, a warehouse
sync — and layers cross-cutting behavior around it. Retry classification.
Wall-clock SLAs. LLM cost caps. PII scrubbing. Point-in-time snapshots.
Whatever your team actually needs.

The point isn't that Dagster should ship every one of these. The point
is that **you don't have to wait for Dagster to ship the one you need.**
Whatever cross-cutting concern is specific to your business — a cost cap
tuned to your LLM budget, a PII scrubber tuned to your compliance rules,
a throttle tuned to your vendor's rate-limit — you can build it once as
a Dagster component and every project on your team gets it, discoverable
in the catalog, composable in YAML, backed by the Dagster event log.

This post is the shape, the four properties that make it scale, and the
seventeen examples I shipped in the community registry this month as
starting points to fork.

## The shape

A Dagster `Component` is just a Python class with a `build_defs` method
that returns a `dg.Definitions` object:

```python
class MyBehavior(dg.Component, dg.Model, dg.Resolvable):
    ...
    def build_defs(self, context) -> dg.Definitions:
        @dg.asset(...)
        def _asset(context):
            ...
        return dg.Definitions(assets=[_asset])
```

Most components use this shape to *create* something new — a warehouse
asset, an ingested DataFrame, a synced workspace object.

But nothing in the shape says the compute has to be new work. If your
component accepts a `compute: {kind: python, python: "my_pkg:my_fn"}`
field, `build_defs` can *import that function* and wrap it with whatever
cross-cutting behavior you want. The resulting `@dg.asset` runs the
user's code, layered inside your logic.

That's a decorator. But it's a decorator that ships with a schema, a
README, an icon, a scaffold command, a spot in the catalog UI — and, as
we'll see, YAML composability that stacks arbitrarily deep without
touching Python.

## Four properties that make this scale

**1. Discoverability.** Team-local `utils/decorators.py` doesn't help
anyone else. A component in the catalog does. Anyone with `dg list
components` finds it. The docs site indexes it. The AI-assistants page
teaches it. It has an install command.

**2. Portability across YAML and Python.** Ship the behavior as a Python
decorator AND as a YAML component, backed by the same helper module.
Python-first callers use `@my_behavior`. YAML-first callers use
`MyBehaviorComponent`. Same behavior, one implementation. Nobody has to
convert.

**3. Event log as state store.** The moment your behavior needs
*cross-run* state — a retry budget, a circuit breaker, a rolling cost
window, a partition lock — you have three bad options: module-level dict
(dies on restart), Redis (new infra), a side database (worse). The
fourth option, which everyone forgets, is `AssetObservation` events in
the Dagster event log. Restart-safe. Worker-safe. Sensor-composable.
Free of new infra. And when the state is an observation, "what has this
thing been doing" is a query, not a debug session.

**4. `wraps:` composability.** This is the property that made me want to
write this post. When your component's compute is a plain user callable
(`compute: {kind: python, python: "..."}`), you can trivially extend the
schema to *accept another component's YAML declaration in place of the
callable*. The outer's `build_defs` calls the inner's `build_defs`,
extracts the inner asset, unwraps its compute function, and re-wraps it
inside the outer's behavior. The customer writes zero Python and gets
this:

```yaml
type: my_company.SnapshotAssetComponent
attributes:
  uri: s3://backups/orders                             # ← YOUR historical audit trail
  wraps:
    type: my_company.BudgetAssetComponent
    attributes:
      max_cost_per_day_usd: 5.00                       # ← YOUR daily API cost cap
      wraps:
        type: my_company.PIIScrubberComponent
        attributes:
          scrub_fields: [email, phone, ssn]            # ← YOUR compliance rules
          wraps:
            type: dagster_community_components.RestApiFetcherComponent
            attributes:
              url: "https://api.example.com/orders"   # ← the actual work
              output_asset_key: orders
```

One asset registered. Snapshotted, cost-capped, PII-scrubbed, and
ingested — every layer plugged in via a YAML block, every layer built by
whoever needs it. Two of these might come from the community registry.
One might be `my_company.PIIScrubberComponent` — internal, tuned to your
compliance rules, but plugs into the same slot. The stack doesn't care.

The Python analog is `@snapshot @budget @scrub_pii def fetch(...)`. Both
shapes are supported. The YAML version is discoverable, schema-validated,
IDE-autocompleted, and reviewable by non-Python folks. The Python
version is what you reach for from a `@dg.asset` you already own.

## What can you build with this?

Anything cross-cutting. A short list of shapes I've seen customers ask
for, that fit this pattern natively:

- **Retry with your team's classification rules.** Retry on `429` from
  vendor X but not vendor Y. Retry on `openai.RateLimitError` but not
  `ValueError`. Retry with different backoff schedules per exception
  class.
- **Cost caps for LLM/API budgets.** Sum `cost_usd` observations over a
  rolling window; short-circuit compute when the window's over budget.
- **PII scrubbing tuned to your compliance rules.** Regex + field list
  applied to `context.log.*` and `MaterializeResult.metadata` before
  they land in the event log.
- **Wall-clock SLAs on any asset's compute.** Emit `sla_breach=true`
  observations that a sensor can escalate on.
- **Rate limiting keyed to your vendor's quotas.** Read the last
  `ASSET_MATERIALIZATION` timestamp; skip or fail if the gap's too
  small.
- **Point-in-time snapshots for regulated data.** Write a
  `code_version`-keyed parquet file to your audit bucket after every
  materialization; rollback becomes an event-log query.
- **Write-audit-publish for critical tables.** Stage to a shadow table,
  run audit checks, promote if they pass, quarantine if not.
- **Circuit breakers.** Count recent failures via event-log query;
  short-circuit compute when the count exceeds threshold.
- **Feature flags for expensive compute.** Dry-run mode that discards
  writes; enable via YAML field, run tag, or env var.
- **Data contracts between producers and consumers.** Emit a contract
  snapshot on producer runs; consumers query the log and refuse to run
  on incompatible upstream versions.
- **Anything else your team has been copy-pasting into `utils/`.**

The common thread: each of these is *specific to your business*. Nobody
outside your team knows what your PII rules are, what your LLM budget is,
what your vendor's actual 429 threshold is. Which means nobody is going
to ship the perfect version in a general-purpose orchestrator library.
But the shape of "wrap someone else's compute + layer behavior on top +
persist state to the event log" is the same regardless — and it's the
part you don't have to figure out on your own.

## Seventeen examples to fork

I built out this whole idea in the community registry as starting
templates. All 17 in the
[`decorator` category](https://dagster-component-ui.vercel.app/?category=decorator):

| Category               | Components                                                        |
|------------------------|-------------------------------------------------------------------|
| Runtime control        | `@cached` · `@budget` · `@sla` · `@timeout` · `@smart_retry` · `@throttle` |
| Safety & lifecycle     | `@shadow` · `@dry_run` · `@snapshot` · `@sensitive` · `@on_hooks` · `@lifecycle` (WAP) |
| Observability          | `@profile` · `@log_prints` · `@data_contract` · `@partition_lock` |
| Dynamic sub-steps      | `@task` / `@task_asset` / `TaskAssetComponent`                    |

Each ships a matched pair — Python decorator + `wraps:`-composable YAML
component — and a runnable walkthrough that reproduces both shapes in
about sixty seconds:

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_<name>_demo.sh | bash
```

Use them as-is if the general form fits, or **fork one, rename it,
replace the middle** with the behavior your team actually needs. The
shape is small — under 40 lines of skeleton per component.

## Building one, end to end

To make the shape concrete, here's what one looks like fully written
out. Same pattern applies to any behavior — replace the middle.

```python
class MyBehaviorAssetComponent(dg.Component, dg.Model, dg.Resolvable):
    asset_name: str
    compute: dict                        # {kind: python, python: "my_pkg:my_fn"}
    # ...your behavior's config fields go here...

    def build_defs(self, context) -> dg.Definitions:
        fn = _load(self.compute)         # import the user's function

        @dg.asset(key=dg.AssetKey([self.asset_name]))
        def _asset(context, **kwargs):
            # ===== BEFORE-COMPUTE HOOK =====
            # e.g. check cost budget, acquire lock, check throttle window
            # emit an AssetObservation for anything sensor-observable

            try:
                result = fn(context, **kwargs)
            except Exception as e:
                # ===== FAILURE HOOK =====
                # e.g. emit failure observation for circuit-breaker state
                raise

            # ===== AFTER-COMPUTE HOOK =====
            # e.g. write snapshot, emit cost, sum against budget, release lock

            return result

        return dg.Definitions(assets=[_asset])
```

That's the whole shape. Everything else is the specific behavior you're
adding.

Two things to know once you're past the skeleton:

**Cross-run state — a query, not a debug session.** If your behavior
needs to know what past runs did (cumulative cost, recent failure count,
last materialization time), emit an `AssetObservation` on each run and
read it back on the next:

```python
from dagster import EventRecordsFilter, DagsterEventType

def _recent_failures(context, window_seconds: int = 600) -> int:
    since = time.time() - window_seconds
    records = context.instance.get_event_records(
        event_records_filter=EventRecordsFilter(
            event_type=DagsterEventType.ASSET_OBSERVATION,
            after_timestamp=since,
        ),
        limit=100, ascending=False,
    )
    return sum(
        1 for r in records
        if r.asset_observation
        and r.asset_observation.tags.get("circuit_breaker") == self.key
        and r.asset_observation.tags.get("outcome") == "failure"
    )
```

That's the whole state store. No Redis. No side database. Restart-safe,
worker-safe, and the same events show up in the Dagster UI + are
queryable by sensors.

**Making it `wraps:`-composable.** Extend the schema to accept another
component's YAML in place of `compute:`:

```python
class MyBehaviorAssetComponent(dg.Component, dg.Model, dg.Resolvable):
    asset_name: str | None = None
    compute: dict | None = None          # {kind: python, python: "..."}
    wraps: dict | None = None            # {type: "...", attributes: {...}}
    # ...your behavior's config fields...
```

In `build_defs`, if `wraps` is set, resolve the inner component, call
its `build_defs`, extract the inner asset, unwrap its compute function,
and re-wrap inside your behavior. Now your component stacks over every
other component in the registry with zero customer Python.

## The Python decorator counterpart

For Python-first callers, ship the same behavior as a decorator:

```python
from my_company import my_behavior

@dg.asset
@my_behavior(...config...)
def some_asset(context):
    return do_the_work()
```

Both paths share the same helper module — the classification logic,
the event-log query, the observation emission. YAML-first teams get
YAML. Python-first teams get Python. Nobody has to convert.

## Try it

Start by browsing the 17 for shapes you recognize:

```bash
dagster-component search "" --category decorator
```

Install one, run its walkthrough, read the source:

```bash
dagster-component add snapshot_asset --auto-install
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_snapshot_asset_demo.sh | bash
```

Then fork the one closest to your use case, rename it, replace the
middle. The shape is small. Your team's cross-cutting concerns don't
have to live in `utils/` anymore.

If you build one, I'd genuinely love to see it.
