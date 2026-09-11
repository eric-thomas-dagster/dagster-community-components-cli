# Track $ per asset — rolling-window cap + pre-flight breach guard
> ✅ **100% offline** — no API keys, no external services.

**Live-validated** — the setup script runs end-to-end demonstrating BOTH
shapes of the DCC decorator API in ~90 seconds.

## What this demo shows

Two assets, one per shape. Both emit the same cost observation format
(`budget_cost_estimate_usd` + `budget_cumulative_usd` + `budget_cap_usd`
metadata + `budget_breach` tag):

| Run | Asset | Cost | Cumulative | Breach? |
|---|---|---|---|---|
| 1 | `py_costly_pipeline` (Python decorator, `cost_fn`) | $0.40 | $0.40 | no |
| 2 | `py_costly_pipeline` (Python decorator, `cost_fn`) | $0.40 | $0.80 | no |
| 3 | `py_costly_pipeline` (Python decorator, `cost_fn`) | $0.40 | $1.20 | **yes** → `budget_breach=true` |
| 4 | `yaml_costly_pipeline` (YAML composability, `cost_per_second`) | ~$1.65 | ~$1.65 | **yes** — inner data-gen wall-clock × $100/s ≫ $1.00 cap |
| 5 | `yaml_costly_pipeline` (YAML composability, `cost_per_second`) | ~$1.48 | ~$3.13 | **yes** — cumulative grows |

Cumulative cost is queried FROM THE EVENT LOG (`AssetObservation` events tagged `budget_cost_asset=<key>` within `window_days`) — no side database.

## The two shapes — same primitive, different authoring surface

**Shape 1: Python decorator with a real `cost_fn`.** Best when your cost model is a function of the compute result (LLM token usage, API events, rows processed):

```python
# src/<pkg>/defs/py_costly_pipeline.py
import dagster as dg
from dagster_community_components import budget

def usd_cost(context, elapsed_s, result) -> float:
    """This shape is what you'd use for OpenAI/Anthropic token pricing —
    multiply result.usage.total_tokens by your provider's price."""
    return 0.40

@dg.asset(group_name="python_decorator")
@budget(cost_fn=usd_cost, budget_usd=1.00, window_days=30, on_breach="warn")
def py_costly_pipeline(context) -> dict:
    return {"processed": 100, "unit_cost_usd": 0.40}
```

**Shape 2: YAML composability — the money shot.** `BudgetAssetComponent` wraps **another DCC component** with a wall-clock cost model. Zero Python for this asset:

```yaml
# src/<pkg>/defs/yaml_costly_pipeline/defs.yaml
type: dagster_community_components.BudgetAssetComponent
attributes:
  budget_usd: 1.00
  cost_per_second: 100.0    # wall-clock rate — contrast with cost_fn above
  window_days: 30
  on_breach: warn
  wraps:
    type: dagster_community_components.SyntheticDataGeneratorComponent
    attributes:
      asset_name: yaml_costly_pipeline
      schema_type: customers
      row_count: 1000
      random_state: 42
```

**One asset is registered** (`yaml_costly_pipeline`) — no duplication. The outer `BudgetAssetComponent` intercepts the inner's compute, times it, computes cost, records it in the event log, and applies breach logic. Inner's config (partitions, deps, kinds, tags, group) all pass through unchanged.

Add or remove the outer wrap without touching the inner's config. Stack arbitrarily deep — `BudgetAssetComponent { wraps: SlaAssetComponent { wraps: SnowflakeQueryComponent } }` = `@budget @sla @snowflake` in Python decorator terms.

## Why the YAML composability is the money shot

Before `wraps:`, YAML users had to write Python callables and reference them via `compute: {kind: python, python: 'mod:fn'}` to use decorators. That's fine but requires user Python.

With `wraps:`, `BudgetAssetComponent` stacks over **any DCC component** with zero user Python:

- `BudgetAssetComponent { wraps: LLMPromptExecutorComponent }` — budget an LLM call
- `BudgetAssetComponent { wraps: SnowflakeQueryComponent }` — budget a Snowflake query (warehouse credits)
- `BudgetAssetComponent { wraps: BigqueryQueryComponent }` — budget a BQ query (slot-seconds)
- `BudgetAssetComponent { wraps: RestApiFetcherComponent }` — budget metered API pulls
- `BudgetAssetComponent { wraps: SyntheticDataGeneratorComponent }` — the demo shape

## Components used

| Component | What it does |
|---|---|
| `budget_asset` (`@budget` decorator + `BudgetAssetComponent`) | Wraps compute with cost tracking + rolling-window cap. `cost_fn(context, elapsed_s, result) -> usd` for accurate per-call cost (LLM/API); `cost_per_second` for wall-clock rate. |
| `synthetic_data_generator` | Inner component the YAML shape wraps — generates realistic customer / order / event data for demos. |

## Why this belongs in Dagster

- **Cost history in the event log** — no side database. FinOps queries are just observation queries.
- **Pre-flight guard** — with `on_breach: "fail"`, the compute is skipped when the cumulative cost has already exceeded budget. Saves the run BEFORE burning.
- **`cost_fn` callback for LLM/API costs** — wall-clock doesn't correlate with $ for token-priced APIs. Supply a function that reads `result.usage.total_tokens` and multiplies by price.

## Three enforcement modes

| Mode | Behavior on breach |
|---|---|
| `warn` (default) | Asset materializes normally. Observation is tagged `budget_breach=true`. Sensors can page. |
| `fail` | Pre-flight check: if cumulative >= budget BEFORE compute, raise `dg.Failure` (saves the run). Also post-flight if this run's cost pushes over. |
| `skip` | Pre-flight check: return `MaterializeResult(budget_skipped=true)` if cumulative >= budget. Silent, sensor-actionable. |

## Cost

**$0.** Fully offline — the demo's Python `cost_fn` returns a hard-coded $0.40, and the YAML wraps uses a wall-clock rate on a local synthetic-data generator.

## Required env vars

None.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_budget_asset_demo.sh | bash
cd budget-asset-demo
uv run dg dev
```

Real cost function shape for LLM/API pricing (OpenAI-style):

```python
def openai_cost(context, elapsed_s, result) -> float:
    tokens = result.get("usage_tokens", 0)
    return tokens * 0.000002   # $2.00 per 1M output tokens
```

## Pair with a sensor

Every breach emits an `AssetObservation` tagged `budget_breach=true`. A sensor that watches for these tags is a page-worthy alert with zero infra:

```python
@dg.sensor(name="budget_alert_sensor")
def budget_alert_sensor(context):
    from dagster import DagsterEventType, EventRecordsFilter
    recs = context.instance.get_event_records(
        event_records_filter=EventRecordsFilter(event_type=DagsterEventType.ASSET_OBSERVATION),
        limit=100, ascending=False,
    )
    for r in recs:
        tags = r.asset_observation.tags or {}
        if tags.get("budget_breach") == "true":
            # ping slack / pagerduty / whatever
            ...
```

## After the demo — inspect in the UI

```bash
cd budget-asset-demo
export DAGSTER_HOME=$(pwd)/.dagster_home
uv run dg dev  # → http://localhost:3000
```

Click either asset → **Observations panel**:
- Every run shows `budget_cost_estimate_usd`, `budget_cumulative_usd`, `budget_cap_usd`, `budget_breach`
- Flip `on_breach: "fail"` → next run raises `dg.Failure` pre-flight (since cumulative is already over the cap)
- Flip to `"skip"` → next run returns cleanly with `budget_skipped=true`

## See also

- [`budget_asset` component reference](https://dagster-component-ui.vercel.app/c/budget_asset)
- [`throttle_asset` walkthrough](throttle_asset.md) — sibling decorator, same `wraps:` shape.
- [`sla_asset` walkthrough](sla_asset.md) — orthogonal — "must finish in <= T seconds" alongside "no more than $X per month".
- Browse the [walkthrough index](README.md) for more decorator + infrastructure demos.
