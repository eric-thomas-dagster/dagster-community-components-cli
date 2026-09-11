# budget_asset — `@budget` decorator: per-asset $ cost tracking + rolling-window cap
> ✅ **100% offline** — no API keys, no cloud, no external services.

**Live-validated** — the setup script runs end-to-end and demonstrates a
full cost-accumulation → breach cycle in ~90 seconds.

## What this demo shows

Three back-to-back runs of one `@budget`-wrapped asset. Each run costs
`$0.40` (via the user-supplied `cost_fn`). The budget cap is `$1.00`
over a rolling 30-day window:

| Run | Cost | Cumulative | Breach? |
|---|---|---|---|
| 1 | $0.40 | $0.40 | no |
| 2 | $0.40 | $0.80 | no |
| 3 | $0.40 | $1.20 | **yes** → observation tagged `budget_breach=true` |

Cumulative cost is queried FROM THE EVENT LOG (`AssetObservation` events
tagged `budget_cost_asset=<key>` within `window_days`) — no side database.

## Components used

| Component | What it does |
|---|---|
| `budget_asset` (`@budget` decorator) | Wraps a `@dg.asset` compute with cost tracking + rolling-window cap. `cost_fn(context, elapsed_s, result) -> usd` for accurate per-call cost (required for LLM/API pricing); `cost_per_second` for wall-clock rate. Companion `BudgetAssetComponent` YAML wrapper. |

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

**$0.** Fully offline — the demo's `cost_fn` returns a hard-coded $0.40.

## Required env vars

None.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_budget_asset_demo.sh | bash
cd budget-asset-demo
uv run dg dev
```

## The decorated asset

```python
# src/<pkg>/defs/costly_pipeline.py
import dagster as dg
from dagster_community_components import budget

def usd_cost(context, elapsed_s, result) -> float:
    """Cost model: this compute costs $0.40 flat (imagine a per-event API price)."""
    return 0.40

@dg.asset(group_name="budget_demo")
@budget(cost_fn=usd_cost, budget_usd=1.00, window_days=30, on_breach="warn")
def costly_pipeline(context) -> dict:
    return {"processed": 100, "unit_cost_usd": 0.40}
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
@dg.sensor
def budget_alert_sensor(context):
    from dagster import DagsterEventType
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

Click `costly_pipeline` → **Observations panel**:
- Every run shows `budget_cost_estimate_usd`, `budget_cumulative_usd`, `budget_cap_usd`, `budget_breach`
- Flip `on_breach: "fail"` in `costly_pipeline.py` → next run raises `dg.Failure` pre-flight (since cumulative is already $1.20)
- Flip to `"skip"` → next run returns cleanly with `budget_skipped=true`

## See also

- [`budget_asset` component reference](https://dagster-component-ui.vercel.app/c/budget_asset)
- Browse the [walkthrough index](README.md) for more decorator + infrastructure demos.
