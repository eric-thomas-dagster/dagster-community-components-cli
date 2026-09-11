# timeout_asset — `@timeout` decorator + `TimeoutAssetComponent.wraps:` composability
> ✅ **100% offline** — no API keys, no external services.

**Live-validated** — the setup script runs end-to-end demonstrating BOTH
shapes of the DCC decorator API in under a minute (plus `uv run dg launch`
startup overhead).

## What this demo shows

Two assets, one per shape. Both hard-kill compute at the deadline and
emit the same `timeout_hit` observations:

| Run | Asset | Behavior |
|---|---|---|
| 1 | `py_slow_api` (Python decorator) | `SLEEP_SECONDS=0.3` < 1.0s timeout → OK |
| 2 | `py_slow_api` (Python decorator) | `SLEEP_SECONDS=2.0` > 1.0s timeout → hard-killed, `dg.Failure` |
| 3 | `yaml_slow_api` (YAML composability) | 5000-row inner data-gen > 10ms timeout → hard-killed, `dg.Failure` |

## The two shapes — same primitive, different authoring surface

**Shape 1: Python decorator** — best when you already have Python code you want to hard-kill:

```python
# src/<pkg>/defs/py_slow_api.py
import os, time
import dagster as dg
from dagster_community_components import timeout

@dg.asset(group_name="python_decorator")
@timeout(1.0, on_timeout="fail", key="py_slow_api")
def py_slow_api(context):        # ← NO `-> dict` annotation:
    sleep_s = float(os.environ.get("SLEEP_SECONDS", "0.3"))
    time.sleep(sleep_s)          #   on_timeout="fail" never returns;
    return {"ok": True}          #   on_timeout="warn" returns None.
                                 #   Either way a `-> dict` would trip Dagster.
```

**Shape 2: YAML composability — the money shot.** `TimeoutAssetComponent` wraps **another DCC component**. Zero Python for this asset — pure YAML stacking. Direct analog of Python `@decorator @dg.asset` idiom, but at the component layer:

```yaml
# src/<pkg>/defs/yaml_slow_api/defs.yaml
type: dagster_community_components.TimeoutAssetComponent
attributes:
  timeout_seconds: 0.01
  on_timeout: fail
  timeout_key: yaml_slow_api_timeout
  wraps:
    type: dagster_community_components.SyntheticDataGeneratorComponent
    attributes:
      asset_name: yaml_slow_api
      schema_type: customers
      row_count: 5000
      random_state: 42
```

**One asset is registered** (`yaml_slow_api`) — no duplication. The outer `TimeoutAssetComponent` intercepts the inner's compute and hard-kills at the deadline. Inner's config (partitions, deps, kinds, tags, group) all pass through unchanged.

Add or remove the outer wrap without touching the inner's config. Stack arbitrarily deep — `SmartRetryAssetComponent { wraps: TimeoutAssetComponent { wraps: SyntheticDataGeneratorComponent { ... } } }` = `@smart_retry @timeout @synthetic-compute` in Python decorator terms.

## Why the YAML composability is the money shot

Before `wraps:`, YAML users had to write Python callables and reference them via `compute: {kind: python, python: 'mod:fn'}` to use decorators. That's fine but requires user Python.

With `wraps:`, decorator components stack over **any DCC component** with zero user Python:

- `TimeoutAssetComponent { wraps: LLMPromptExecutorComponent }` — hard-kill an LLM call that hangs
- `TimeoutAssetComponent { wraps: BigqueryQueryComponent }` — hard-kill a runaway warehouse query
- `TimeoutAssetComponent { wraps: RestApiFetcherComponent }` — hard-kill a stuck external API call
- `TimeoutAssetComponent { wraps: DockerContainerAsset }` — hard-kill a container asset that ran past budget
- `SmartRetryAssetComponent { wraps: TimeoutAssetComponent { wraps: SnowflakeQueryComponent } }` — retry on timeout with backoff

## What this does that `@sla` doesn't

| | `@sla` | `@timeout` |
|---|---|---|
| Timer wraps compute | ✓ | ✓ |
| Observes overrun | ✓ | ✓ |
| Asset materializes past deadline | ✓ (unless `fail`) | ✗ (killed) |
| Prevents runaway budget burn | ✗ | ✓ |
| Cross-run breach tracking | ✓ | ✓ |

Use both together: `@sla(expected_duration=30)` for observation + `@timeout(60)` for the hard limit.

## Components used

| Component | What it does |
|---|---|
| `timeout_asset` (`@timeout` decorator + `TimeoutAssetComponent`) | Hard-kill compute at N seconds via `concurrent.futures.ThreadPoolExecutor + future.result(timeout=...)`. Portable across every Dagster deployment shape (Serverless-safe — no `signal.SIGALRM` dependency). `on_timeout: fail` (default) raises `dg.Failure`; `on_timeout: warn` logs + returns None. |
| `synthetic_data_generator` | Inner component the YAML shape wraps — generates realistic customer/order/event data for demos. |

## Why this belongs in Dagster

- **Fills a real gap in `RetryPolicy`** — Dagster's built-in retry has NO timeout knob. `@timeout` is the missing piece.
- **Same primitive, different surface** — Python + YAML users get identical behavior. Same observation events. Same sensor targets.
- **Composability at the component layer** — you can wrap ANY DCC component with the timeout behavior without editing that component's config.
- **Portable hard-kill** — ThreadPool approach works on every Dagster deployment shape (Serverless, K8s, Docker, local). Signal-based approaches only work on Unix main-thread.

**Caveat:** Python threads can't be truly killed. The cancelled compute keeps running in the background but its result is discarded — on a well-behaved compute this is fine; on a stuck one you leak a thread until process exit. The deadline is enforced from Dagster's perspective.

## Cost

**$0.** Fully offline.

## Required env vars

None. The demo sets `SLEEP_SECONDS` inline per run.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_timeout_asset_demo.sh | bash
cd timeout-asset-demo
uv run dg dev
```

## Pair with a sensor

```python
@dg.sensor(name="timeout_alert")
def timeout_alert(context):
    from dagster import DagsterEventType, EventRecordsFilter
    recs = context.instance.get_event_records(
        event_records_filter=EventRecordsFilter(event_type=DagsterEventType.ASSET_OBSERVATION),
        limit=100, ascending=False,
    )
    timeouts = [r for r in recs if (r.asset_observation.tags or {}).get("timeout_hit")]
    if len(timeouts) >= 3:
        # 3+ timeouts recently — page oncall
        ...
```

## After the demo — inspect in the UI

```bash
cd timeout-asset-demo
export DAGSTER_HOME=$(pwd)/.dagster_home
uv run dg dev
```

Click either asset → **Observations panel** shows `timeout_hit` tag + `timeout_seconds` metadata for each deadline-exceeded attempt.

## See also

- [`timeout_asset` component reference](https://dagster-component-ui.vercel.app/c/timeout_asset)
- [`sla_asset` walkthrough](sla_asset.md) — observe overrun without killing; use together with `@timeout` for observe-at-N + kill-at-M
- [`throttle_asset` walkthrough](throttle_asset.md) — same "wraps" pattern, rate-limiting primitive
- [`smart_retry` walkthrough](smart_retry.md) — retry on timeout by classifying `TimeoutError` as transient
- Browse the [walkthrough index](README.md) for more decorator + infrastructure demos.
