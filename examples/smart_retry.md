# smart_retry — `@smart_retry` decorator: classification-aware retry (transient vs permanent)
> ✅ **100% offline** — no API keys, no external services.

**Live-validated** — the setup script runs end-to-end and demonstrates
three classification outcomes in ~2 minutes (includes real Dagster step retries).

## What this demo shows

Three runs of one `@smart_retry`-wrapped asset, controlled by a `MODE` env var:

| Run | MODE | Behavior | Outcome |
|---|---|---|---|
| 1 | `success` | 1 attempt | OK |
| 2 | `flaky` | raises `ConnectionError` (transient) 2× → succeeds on attempt 3 | OK after retries |
| 3 | `permanent` | raises `ValueError` (permanent) | **fails immediately, no retry** |

The insight: classification. A `ValueError` gets flagged **permanent** and fails fast. A `ConnectionError` gets flagged **transient** and rides the backoff loop.

## Components used

| Component | What it does |
|---|---|
| `smart_retry` (`@smart_retry` decorator) | Wraps any compute with classification-aware retry. Rules classify exceptions as `transient` (retry with backoff) or `permanent` (raise `dg.Failure` immediately). Optional day-1 features: LLM-classifier fallback for unmatched exceptions, cross-run rate limiter, circuit breaker. Companion `SmartRetryComponent` YAML wrapper. |

## Why this belongs in Dagster (over the built-in `RetryPolicy`)

Dagster's built-in `RetryPolicy(max_retries=N, delay=D, backoff=B)` retries ANY failure N times. That's the wrong shape when:

- **You have a permanent bug** — retrying a `ValueError` 5× wastes 5× the compute and delays the real failure signal.
- **The remote API is telling you "not now" vs "no"** — HTTP 429 is transient, HTTP 400 is permanent. `RetryPolicy` can't distinguish.
- **You need a circuit breaker** — retry loops that thrash a degraded downstream make things worse, not better.

`@smart_retry` classifies each exception before deciding. Rules are declarative:

```python
rules=[
    {"kind": "exception_class",
     "transient": ["ConnectionError", "TimeoutError"],
     "permanent": ["ValueError", "KeyError"]},
    {"kind": "http_status",
     "transient_codes": [429, 500, 502, 503, 504],
     "permanent_codes": [400, 401, 403, 404]},
]
```

Unmatched exceptions default to transient (safe default) — override with `default: "permanent"` per rule.

## Cost

**$0.** Fully offline (the failure modes are simulated in a local Python function).

## Required env vars

None (demo controls behavior via `MODE`).

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_smart_retry_demo.sh | bash
cd smart-retry-demo
uv run dg dev
```

## The decorated asset

```python
# src/<pkg>/defs/api_call.py
import os
import dagster as dg
from dagster_community_components import smart_retry

@dg.asset(group_name="retry_demo")
@smart_retry(
    rules=[
        {"kind": "exception_class",
         "transient": ["ConnectionError", "TimeoutError"],
         "permanent": ["ValueError", "KeyError"]},
    ],
    max_attempts=5,
    backoff="exponential",
    initial_delay_seconds=1.0,
    max_delay_seconds=60.0,
    jitter=True,
    key="api_call_retry",   # shared state key for rate_limit + circuit_breaker
)
def api_call(context) -> dict:
    ...   # existing code, unchanged. smart_retry wraps it.
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

## Under the hood — how retries fire

`@smart_retry` raises `dagster.RetryRequested(...)` to trigger a real Dagster step retry. That means:

- The run graph shows `attempt # 2 / 3 / ...` badges
- Each attempt gets its own worker step (visible in the UI's step timeline)
- Backoff is enforced BEFORE the next attempt via `seconds_to_wait=`
- If `max_attempts` is exhausted → `dg.Failure` with attempt-count metadata

Permanent classification → `dg.Failure` immediately, no `RetryRequested`. That's what stops thrashing.

## Pair with a sensor

Every classified failure emits an `AssetObservation` tagged `smart_retry_failure=<key>` with `ts` metadata. A sensor watching for repeated failures within a window is a page-worthy alert. Circuit-breaker OPEN state also emits an observation tagged `smart_retry_open=<key>` — useful for surfacing "we've stopped retrying downstream X" to operators.

## After the demo — inspect in the UI

```bash
cd smart-retry-demo
export DAGSTER_HOME=$(pwd)/.dagster_home
uv run dg dev
```

Click `api_call` → **Runs tab** → each row shows the attempt count. RUN 2 (flaky) shows 3 attempts, RUN 3 (permanent) shows 1 attempt + failure.

## See also

- [`smart_retry` component reference](https://dagster-component-ui.vercel.app/c/smart_retry)
- [`sla_asset` walkthrough](sla_asset.md) — pair with SLA to enforce total-wall-clock even under retries.
- Browse the [walkthrough index](README.md) for more decorator + infrastructure demos.
