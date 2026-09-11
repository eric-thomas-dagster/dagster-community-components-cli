# throttle_asset — `@throttle` decorator + `ThrottleAssetComponent.wraps:` composability
> ✅ **100% offline** — no API keys, no external services.

**Live-validated** — the setup script runs end-to-end demonstrating BOTH
shapes of the DCC decorator API in ~4 minutes (includes real throttle wait).

## What this demo shows

Two assets, one per shape. Both share the same event-log-backed
rate-limit state and emit the same throttle observations:

| Run | Asset | Behavior |
|---|---|---|
| 1 | `py_report` (Python decorator) | 1st materialization → OK |
| 2 | `py_report` (Python decorator) | <60s later → THROTTLED |
| 3 | `yaml_report` (YAML composability) | 1st materialization → inner data-gen runs (1000 customer rows) |
| 4 | `yaml_report` (YAML composability) | <60s later → THROTTLED (inner data-gen NOT invoked) |
| 5 | `yaml_report` (YAML composability) | >60s wait → inner data-gen runs again |

## The two shapes — same primitive, different authoring surface

**Shape 1: Python decorator** — best when you already have Python code you want to rate-limit:

```python
# src/<pkg>/defs/py_report.py
import dagster as dg
from dagster_community_components import throttle

@dg.asset(group_name="python_decorator")
@throttle(min_gap_seconds=60, on_throttle="skip", key="py_report")
def py_report(context):        # ← NO `-> dict` annotation:
    return {...}               #   on_throttle="skip" returns None,
                               #   Dagster's type check would fail.
```

**Shape 2: YAML composability — the money shot.** `ThrottleAssetComponent` wraps **another DCC component**. Zero Python for this asset — pure YAML stacking. Direct analog of Python `@decorator @dg.asset` idiom, but at the component layer:

```yaml
# src/<pkg>/defs/yaml_report/defs.yaml
type: dagster_community_components.ThrottleAssetComponent
attributes:
  min_gap_seconds: 60
  on_throttle: skip
  key: yaml_report_throttle
  wraps:
    type: dagster_community_components.SyntheticDataGeneratorComponent
    attributes:
      asset_name: yaml_report
      schema_type: customers
      row_count: 1000
      random_state: 42
```

**One asset is registered** (`yaml_report`) — no duplication. The outer `ThrottleAssetComponent` intercepts the inner's compute and applies throttle logic. Inner's config (partitions, deps, kinds, tags, group) all pass through unchanged.

Add or remove the outer wrap without touching the inner's config. Stack arbitrarily deep — `BudgetAssetComponent { wraps: SlaAssetComponent { wraps: SyntheticDataGeneratorComponent { ... } } }` = `@budget @sla @synthetic-compute` in Python decorator terms.

## Why the YAML composability is the money shot

Before `wraps:`, YAML users had to write Python callables and reference them via `compute: {kind: python, python: 'mod:fn'}` to use decorators. That's fine but requires user Python.

With `wraps:`, decorator components stack over **any DCC component** with zero user Python:

- `BudgetAssetComponent { wraps: LLMPromptExecutorComponent }` — budget an LLM call
- `SlaAssetComponent { wraps: BigqueryQueryComponent }` — SLA a warehouse query
- `CachedAssetComponent { wraps: SnowflakeQueryComponent }` — cache a warehouse query result
- `ThrottleAssetComponent { wraps: StripeIngestion }` — rate-limit external API calls
- `ShadowAssetComponent { wraps: NewVendorComponent, shadow: OldVendorComponent }` — dual-run vendor swap

## Components used

| Component | What it does |
|---|---|
| `throttle_asset` (`@throttle` decorator + `ThrottleAssetComponent`) | Cross-run rate limiting. State lives in the Dagster event log (last successful materialization timestamp) — no Redis, no cross-process coordination. `on_throttle: skip` (default) returns None + observation; `on_throttle: fail` raises `dg.Failure`. |
| `synthetic_data_generator` | Inner component the YAML shape wraps — generates realistic customer/order/event data for demos. |

## Why this belongs in Dagster

- **State-free rate limiting** — event log is the source of truth. Works across workers, restarts, concurrent runs.
- **Same primitive, different surface** — Python + YAML users get identical behavior. Same observation events. Same sensor targets.
- **Composability at the component layer** — you can wrap ANY DCC component with the throttle behavior without editing that component's config.

## Cost

**$0.** Fully offline.

## Required env vars

None.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_throttle_asset_demo.sh | bash
cd throttle-asset-demo
uv run dg dev
```

## Pair with a sensor

```python
@dg.sensor(name="throttle_rate_watcher")
def throttle_rate_watcher(context):
    from dagster import DagsterEventType, EventRecordsFilter
    recs = context.instance.get_event_records(
        event_records_filter=EventRecordsFilter(event_type=DagsterEventType.ASSET_OBSERVATION),
        limit=100, ascending=False,
    )
    throttled = [r for r in recs if (r.asset_observation.tags or {}).get("throttle_skipped")]
    if len(throttled) > 10:
        # ping oncall — the pipeline is being invoked too aggressively
        ...
```

## After the demo — inspect in the UI

```bash
cd throttle-asset-demo
export DAGSTER_HOME=$(pwd)/.dagster_home
uv run dg dev
```

Click either asset → **Observations panel** shows `throttle_skipped` tag + `throttle_wait_seconds` metadata for each blocked attempt.

## See also

- [`throttle_asset` component reference](https://dagster-component-ui.vercel.app/c/throttle_asset)
- [`sla_asset` walkthrough](sla_asset.md) — also supports `wraps:` composability.
- [`budget_asset` walkthrough](budget_asset.md) — orthogonal — "no more than $X per month" alongside "no more than N per hour".
- Browse the [walkthrough index](README.md) for more decorator + infrastructure demos.
