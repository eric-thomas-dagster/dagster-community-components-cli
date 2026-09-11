# sla_asset — `@sla` decorator + `SlaAssetComponent.wraps:` composability
> ✅ **100% offline** — no API keys, no external services.

**Live-validated** — the setup script runs end-to-end demonstrating BOTH
shapes of the DCC decorator API in ~90 seconds.

## What this demo shows

Two assets, one per shape. Both emit the same breach observation format
(`sla_actual_seconds` / `sla_expected_seconds` / `sla_overrun_pct` +
`sla_breach` / `sla_escalated` tags):

| Run | Asset | Behavior |
|---|---|---|
| 1 | `py_slow_report` (Python decorator) | 0.2s < 0.5s SLA → OK, no breach |
| 2 | `py_slow_report` (Python decorator) | 1.0s > 0.5s SLA → BREACH #1 |
| 3 | `py_slow_report` (Python decorator) | 1.0s > 0.5s SLA → BREACH #2 |
| 4 | `py_slow_report` (Python decorator) | 1.0s > 0.5s SLA → BREACH #3 → **`sla_escalated=true`** |
| 5 | `yaml_slow_report` (YAML composability) | 5000-row synthetic data-gen > 0.001s SLA → BREACH; inner component ran |
| 6 | `yaml_slow_report` (YAML composability) | same — inner data-gen still runs even on breach (`on_breach=warn`) |

Escalation counting is cross-run — the `@sla` decorator queries the event log via `context.instance.get_event_records` for prior breach observations within `escalate_window_seconds`. No external state.

## The two shapes — same primitive, different authoring surface

**Shape 1: Python decorator** — best when you already have Python code you want to time:

```python
# src/<pkg>/defs/py_slow_report.py
import os, time
import dagster as dg
from dagster_community_components import sla

@dg.asset(group_name="python_decorator")
@sla(
    expected_duration_seconds=0.5,
    on_breach="warn",
    escalate_after_n_breaches=3,
    escalate_window_seconds=3600,
    key="py_slow_report_sla",
)
def py_slow_report(context):
    sleep_s = float(os.environ.get("SLEEP_SECONDS", "0.2"))
    time.sleep(sleep_s)
    return {"rows_processed": 100}
```

**Shape 2: YAML composability — the money shot.** `SlaAssetComponent` wraps **another DCC component**. Zero Python for this asset — pure YAML stacking. Direct analog of Python `@sla @dg.asset` idiom, but at the component layer:

```yaml
# src/<pkg>/defs/yaml_slow_report/defs.yaml
type: dagster_community_components.SlaAssetComponent
attributes:
  expected_duration_seconds: 0.001    # deliberately tight → always breaches
  on_breach: warn
  escalate_after_n_breaches: 3
  escalate_window_seconds: 3600
  sla_key: yaml_slow_report_sla
  wraps:
    type: dagster_community_components.SyntheticDataGeneratorComponent
    attributes:
      asset_name: yaml_slow_report
      schema_type: customers
      row_count: 5000
      random_state: 42
```

**One asset is registered** (`yaml_slow_report`) — no duplication. The outer `SlaAssetComponent` intercepts the inner's compute and applies the SLA timer + breach observation. Inner's config (partitions, deps, kinds, tags, group) all pass through unchanged.

Add or remove the outer wrap without touching the inner's config. Stack arbitrarily deep — `BudgetAssetComponent { wraps: SlaAssetComponent { wraps: SyntheticDataGeneratorComponent { ... } } }` = `@budget @sla @synthetic-compute` in Python decorator terms.

## Why the YAML composability is the money shot

Before `wraps:`, YAML users had to write Python callables and reference them via `compute: {kind: python, python: 'mod:fn'}` to use decorators. That's fine but requires user Python.

With `wraps:`, `SlaAssetComponent` stacks over **any DCC component** with zero user Python:

- `SlaAssetComponent { wraps: BigqueryQueryComponent }` — SLA a warehouse query
- `SlaAssetComponent { wraps: SnowflakeQueryComponent }` — SLA a Snowflake CTAS
- `SlaAssetComponent { wraps: LLMPromptExecutorComponent }` — SLA an LLM call
- `SlaAssetComponent { wraps: RestApiFetcherComponent }` — SLA an external API pull
- `SlaAssetComponent { wraps: SyntheticDataGeneratorComponent }` — the demo shape

## Components used

| Component | What it does |
|---|---|
| `sla_asset` (`@sla` decorator + `SlaAssetComponent`) | Wraps compute with a wall-clock timer. On overrun, emits `AssetObservation(sla_breach=<key>, sla_actual_seconds, sla_expected_seconds, sla_overrun_pct, sla_escalated)`. Escalates after N breaches within `escalate_window_seconds`. |
| `synthetic_data_generator` | Inner component the YAML shape wraps — generates realistic customer / order / event data for demos. |

## Why this belongs in Dagster

`@dg.asset(freshness_policy=...)` governs **source freshness** ("when should the upstream data be refreshed"). This is different: `@sla` enforces **wall-clock compute duration** ("this compute should finish in <= 60 seconds"). Complementary, not redundant.

Every primitive rides on Dagster events:
- **Breach event** → `AssetObservation` with typed `MetadataValue`
- **Cross-run breach history** → `context.instance.get_event_records` filtered on the breach tag
- **Escalation** → observation tags; sensors watch them
- **UI trend** → the observation panel shows SLA overrun % over time

## Two on_breach modes

| Mode | Behavior |
|---|---|
| `warn` (default) | Asset materializes normally + breach observation |
| `fail` | Raise `dg.Failure` with breach metadata AFTER compute (compute time already spent, but downstream doesn't run) |

## Cost

**$0.** Fully offline.

## Required env vars

None (demo controls behavior via `SLEEP_SECONDS`).

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_sla_asset_demo.sh | bash
cd sla-asset-demo
uv run dg dev
```

## Pair with a sensor

The escalation tag is what makes `@sla` page-worthy without any extra plumbing:

```python
@dg.sensor(name="sla_escalation_alert")
def sla_escalation_alert(context):
    from dagster import DagsterEventType, EventRecordsFilter
    recs = context.instance.get_event_records(
        event_records_filter=EventRecordsFilter(event_type=DagsterEventType.ASSET_OBSERVATION),
        limit=50, ascending=False,
    )
    for r in recs:
        tags = r.asset_observation.tags or {}
        if tags.get("sla_escalated") == "true":
            # page oncall
            ...
```

## After the demo — inspect in the UI

```bash
cd sla-asset-demo
export DAGSTER_HOME=$(pwd)/.dagster_home
uv run dg dev
```

Click either asset → **Observations panel** shows `sla_actual_seconds`, `sla_expected_seconds`, `sla_overrun_pct`, `sla_escalated`.

## See also

- [`sla_asset` component reference](https://dagster-component-ui.vercel.app/c/sla_asset)
- [`throttle_asset` walkthrough](throttle_asset.md) — sibling decorator, same `wraps:` shape.
- [`budget_asset` walkthrough](budget_asset.md) — orthogonal — "no more than $X per month" alongside "must finish in <= T seconds".
- [`smart_retry` walkthrough](smart_retry.md) — pair with SLA to enforce total-wall-clock even under retries.
- Browse the [walkthrough index](README.md) for more decorator + infrastructure demos.
