# Classification-aware retry — transient vs permanent, no thrashing
> ✅ **100% offline** — no API keys, no external services.

**Live-validated** — the setup script runs end-to-end demonstrating BOTH
shapes of the DCC decorator API in ~2 minutes (includes real Dagster step retries).

## What this demo shows

Two assets, one per shape. Both use the same classification engine
(transient → `RetryRequested` → real Dagster step restart; permanent →
`dg.Failure` immediately, no retry):

| Run | Asset | Behavior | Outcome |
|---|---|---|---|
| 1 | `py_api_call` (Python decorator, `MODE=success`) | 1 attempt | OK |
| 2 | `py_api_call` (Python decorator, `MODE=flaky`) | raises `ConnectionError` (transient) 2× → succeeds on attempt 3 (real `STEP_RESTARTED`) | OK after retries |
| 3 | `py_api_call` (Python decorator, `MODE=permanent`) | raises `ValueError` (permanent) | **fails immediately, no retry** |
| 4 | `yaml_api_call` (YAML composability) | inner `SyntheticDataGeneratorComponent` won't fail → wrap loads + runs cleanly (retry layered on top) | OK |

The insight: **classification**. A `ValueError` gets flagged **permanent** and fails fast. A `ConnectionError` gets flagged **transient** and rides the backoff loop via Dagster's real `STEP_RESTARTED` machinery.

## The two shapes — same primitive, different authoring surface

**Shape 1: Python decorator** — best when you already have Python code you want to add retry-classification to:

```python
# src/<pkg>/defs/py_api_call.py
import os
import dagster as dg
from dagster_community_components import smart_retry

@dg.asset(group_name="python_decorator")
@smart_retry(
    rules=[
        {"kind": "exception_class",
         "transient": ["ConnectionError", "TimeoutError"],
         "permanent": ["ValueError", "KeyError"]},
    ],
    max_attempts=5,
    backoff="fixed",
    initial_delay_seconds=0.2,
    key="py_api_call_retry",
)
def py_api_call(context) -> dict:
    mode = os.environ.get("MODE", "success")
    ...
```

**Shape 2: YAML composability — the money shot.** `SmartRetryComponent` wraps **another DCC component**. Zero Python for this asset — pure YAML stacking. Direct analog of Python `@smart_retry @dg.asset` idiom, but at the component layer:

```yaml
# src/<pkg>/defs/yaml_api_call/defs.yaml
type: dagster_community_components.SmartRetryComponent
attributes:
  retry_rules:
    - kind: http_status
      transient_codes: [429, 500, 502, 503, 504]
      permanent_codes: [400, 401, 403, 404, 422]
    - kind: exception_class
      transient: [ConnectionError, TimeoutError]
      permanent: [ValueError, KeyError]
  retry_policy:
    max_attempts: 5
    backoff: fixed
    initial_delay_seconds: 0.2
  wraps:
    type: dagster_community_components.SyntheticDataGeneratorComponent
    attributes:
      asset_name: yaml_api_call
      schema_type: customers
      row_count: 500
      random_state: 42
```

**One asset is registered** (`yaml_api_call`) — no duplication. The outer `SmartRetryComponent` intercepts the inner's compute; if the inner raises, the outer classifies via the `retry_rules` and either issues a `RetryRequested` (transient) or raises `dg.Failure` (permanent). Inner's config (partitions, deps, kinds, tags, group) all pass through unchanged.

The demo's inner `SyntheticDataGeneratorComponent` won't fail on its own — this run demonstrates the wrap **loads + runs cleanly**. In production the inner is typically a component that CAN fail (`rest_api_fetcher`, `snowflake_query`, `mongodb_writer`, etc.) — that's when classification kicks in.

Add or remove the outer wrap without touching the inner's config. Stack arbitrarily deep — `SlaAssetComponent { wraps: SmartRetryComponent { wraps: RestApiFetcherComponent } }` = `@sla @smart_retry @fetch` in Python decorator terms.

## Why the YAML composability is the money shot

Before `wraps:`, YAML users had to write Python callables and reference them via `compute: {kind: python, python: 'mod:fn'}` to use decorators. That's fine but requires user Python.

With `wraps:`, `SmartRetryComponent` stacks over **any DCC component** with zero user Python:

- `SmartRetryComponent { wraps: RestApiFetcherComponent }` — 429/500 → transient; 400/404 → permanent
- `SmartRetryComponent { wraps: SnowflakeQueryComponent }` — auth errors → permanent; timeouts → transient
- `SmartRetryComponent { wraps: MongodbWriterComponent }` — network blips → transient; schema violations → permanent
- `SmartRetryComponent { wraps: LLMPromptExecutorComponent }` — 429 rate-limit → transient; 401 bad key → permanent
- `SmartRetryComponent { wraps: SyntheticDataGeneratorComponent }` — the demo shape (verifies the wrap runs cleanly)

## Components used

| Component | What it does |
|---|---|
| `smart_retry` (`@smart_retry` decorator + `SmartRetryComponent`) | Wraps compute with classification-aware retry. Rules classify exceptions as `transient` (retry with backoff via real `STEP_RESTARTED`) or `permanent` (raise `dg.Failure` immediately). Optional day-1 features: LLM-classifier fallback for unmatched exceptions, cross-run rate limiter, circuit breaker. |
| `synthetic_data_generator` | Inner component the YAML shape wraps — generates realistic customer / order / event data for demos. |

## Why this belongs in Dagster (over the built-in `RetryPolicy`)

Dagster's built-in `RetryPolicy(max_retries=N, delay=D, backoff=B)` retries ANY failure N times. That's the wrong shape when:

- **You have a permanent bug** — retrying a `ValueError` 5× wastes 5× the compute and delays the real failure signal.
- **The remote API is telling you "not now" vs "no"** — HTTP 429 is transient, HTTP 400 is permanent. `RetryPolicy` can't distinguish.
- **You need a circuit breaker** — retry loops that thrash a degraded downstream make things worse, not better.

`@smart_retry` classifies each exception before deciding. Rules are declarative — see the YAML above for the full shape.

Unmatched exceptions default to transient (safe default) — override with `default: "permanent"` per rule.

## Under the hood — how retries fire

`@smart_retry` raises `dagster.RetryRequested(...)` to trigger a real Dagster step retry. That means:

- The run graph shows `attempt # 2 / 3 / ...` badges
- Each attempt gets its own worker step (visible in the UI's step timeline)
- Backoff is enforced BEFORE the next attempt via `seconds_to_wait=`
- If `max_attempts` is exhausted → `dg.Failure` with attempt-count metadata

Permanent classification → `dg.Failure` immediately, no `RetryRequested`. That's what stops thrashing.

## Cost

**$0.** Fully offline (Shape 1 simulates failure modes in a local Python function; Shape 2 wraps a local synthetic data generator).

## Required env vars

None (Shape 1 controls behavior via `MODE`).

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_smart_retry_demo.sh | bash
cd smart-retry-demo
uv run dg dev
```

## Day-1 opt-in features (all off by default)

```python
@smart_retry(
    rules=[...],
    max_attempts=5,
    # LLM classifies unmatched exceptions as transient/permanent
    llm_fallback={
        "model": "gpt-4o-mini",
        "api_key_env_var": "OPENAI_API_KEY",
    },
    # Sliding-window rate limit — prevent retry-storm spend
    rate_limit={
        "max_events": 10,
        "window_seconds": 60,
        "mode": "fail",   # or "wait"
    },
    # Circuit breaker — fail fast when downstream is degraded
    circuit_breaker={
        "threshold": 5,
        "observation_window_seconds": 300,
        "cooldown_seconds": 60,
    },
)
def api_call(context):
    ...
```

## Pair with a sensor

Every classified failure emits an `AssetObservation` tagged `smart_retry_failure=<key>` with `ts` metadata. A sensor watching for repeated failures within a window is a page-worthy alert. Circuit-breaker OPEN state also emits an observation tagged `smart_retry_open=<key>` — useful for surfacing "we've stopped retrying downstream X" to operators.

## After the demo — inspect in the UI

```bash
cd smart-retry-demo
export DAGSTER_HOME=$(pwd)/.dagster_home
uv run dg dev
```

Click `py_api_call` → **Runs tab** → each row shows the attempt count. RUN 2 (flaky) shows 3 attempts, RUN 3 (permanent) shows 1 attempt + failure.

## See also

- [`smart_retry` component reference](https://dagster-component-ui.vercel.app/c/smart_retry)
- [`throttle_asset` walkthrough](throttle_asset.md) — sibling decorator, same `wraps:` shape.
- [`sla_asset` walkthrough](sla_asset.md) — pair with SLA to enforce total-wall-clock even under retries.
- [`budget_asset` walkthrough](budget_asset.md) — retries still count toward the budget.
- Browse the [walkthrough index](README.md) for more decorator + infrastructure demos.
