# hooks_asset — `@on_hooks` decorator + `HooksAssetComponent.wraps:` composability
> ✅ **100% offline** — no API keys, no external services.

**Live-validated** — the setup script runs end-to-end demonstrating BOTH
shapes of the DCC decorator API (including a real failure path) in ~2 minutes.

## What this demo shows

Two shapes, same on_success / on_failure callback wiring. Callbacks are
plain Python `mod:fn` references so both the Python decorator and the YAML
component point at the same shared file:

| Run | Asset | Behavior |
|---|---|---|
| 1 | `py_success` (Python decorator) | Compute succeeds → both `notify_success` + `audit_log` hooks fire (multiple hooks fire in list order) |
| 2 | `py_failure` (Python decorator) | Compute raises `RuntimeError` → `notify_failure` fires; original error re-raised → asset STEP_FAILURE |
| 3 | `yaml_customers` (YAML composability) | Inner `SyntheticDataGeneratorComponent` runs (100 customer rows); on_success hooks fire with the returned DataFrame |

## The two shapes — same primitive, different authoring surface

Shared callback module — one file both shapes reference:

```python
# src/<pkg>/hooks.py
def notify_success(context, result):
    context.log.info(f"[hook.notify_success] asset materialized OK; result_type={type(result).__name__}")

def notify_failure(context, exc):
    context.log.error(f"[hook.notify_failure] asset FAILED with {type(exc).__name__}: {exc}")

def audit_log(context, result):
    context.log.info(f"[hook.audit_log] compliance/audit log entry written")
```

**Shape 1: Python decorator** — best when you already have Python code:

```python
# src/<pkg>/defs/py_success.py
import dagster as dg
from dagster_community_components import on_hooks

@dg.asset(group_name="python_decorator")
@on_hooks(
    on_success=["<pkg>.hooks:notify_success", "<pkg>.hooks:audit_log"],
    on_failure=["<pkg>.hooks:notify_failure"],
)
def py_success(context):
    return {"status": "ok", "rows": 42}
```

**Signatures:**
- `on_success(context, result) -> None` — fires after successful return
- `on_failure(context, exception) -> None` — fires on raise; then re-raised (compute failure preserved)

Callback exceptions are **LOGGED** (not re-raised) — hooks NEVER change the compute's outcome. Matches Prefect's semantics.

**Shape 2: YAML composability — the money shot.** `HooksAssetComponent` wraps **another DCC component**. After the inner materializes, the on_success callbacks fire (in list order) with `(context, result_of_inner_compute)`:

```yaml
# src/<pkg>/defs/yaml_customers/defs.yaml
type: dagster_community_components.HooksAssetComponent
attributes:
  on_success:
    - "<pkg>.hooks:notify_success"
    - "<pkg>.hooks:audit_log"
  on_failure:
    - "<pkg>.hooks:notify_failure"
  wraps:
    type: dagster_community_components.SyntheticDataGeneratorComponent
    attributes:
      asset_name: yaml_customers
      schema_type: customers
      row_count: 100
      random_state: 42
```

**One asset is registered** (`yaml_customers`) — no duplication. Add or remove the outer wrap without touching the inner's config. Stack arbitrarily deep — `HooksAssetComponent { wraps: SlaAssetComponent { wraps: SnowflakeQueryComponent { ... } } }` = `@on_hooks @sla @snowflake-compute` in Python decorator terms.

## Why the YAML composability is the money shot

Before `wraps:`, YAML users had to write Python callables and reference them via `compute: {kind: python, python: 'mod:fn'}` to use decorators. That's fine but requires user Python.

With `wraps:`, `HooksAssetComponent` stacks over **any DCC component** with zero user Python (aside from the callback module itself — which is shared across every asset that uses hooks):

- `HooksAssetComponent { on_failure: [page_oncall], wraps: SnowflakeQueryComponent }` — page on warehouse query failure
- `HooksAssetComponent { on_success: [notify_slack], wraps: DatabaseReplicationComponent }` — post to Slack on successful replication
- `HooksAssetComponent { on_failure: [create_jira_ticket], wraps: LlmPromptExecutorComponent }` — open ticket on LLM call failure
- `HooksAssetComponent { on_success: [update_freshness_dashboard], wraps: KafkaToDatabaseAssetComponent }` — post freshness update on stream ingest success

## Asset-scoped hooks vs Dagster's built-in job hooks

Dagster's built-in `@dg.success_hook` / `@dg.failure_hook` are **job-scoped** — you attach them to a job's ops in the job wiring:

```python
# Dagster core — job-scoped
@dg.success_hook
def on_success(context): ...

my_job = my_asset.to_source_asset().define_asset_job("my_job").with_hooks({on_success})
```

That works when you have a job with multiple ops. For **asset-first projects** — where every asset is its own compute and you want the callback right next to the asset — `@on_hooks` / `HooksAssetComponent` is the right shape. The hooks live with the asset, not off to the side in job wiring.

## Components used

| Component | What it does |
|---|---|
| `hooks_asset` (`@on_hooks` decorator + `HooksAssetComponent`) | Asset-scoped success / failure callbacks. Callbacks are `mod:fn` refs, called with `(context, result)` on success or `(context, exception)` on failure. Multiple callbacks per outcome; fire in list order. Callback exceptions are logged, not re-raised. |
| `synthetic_data_generator` | Inner component the YAML shape wraps. |

## Why this belongs in Dagster

- **Asset-first callbacks** — the callback lives with the asset, not off to the side in job wiring.
- **Shared callback module** — one `src/<pkg>/hooks.py` that both Python + YAML shapes reference. Every team's slack/jira/pagerduty adapter written once.
- **Composability at the component layer** — wrap ANY DCC component with hooks without editing that component's config.
- **Prefect-compatible semantics** — callback exceptions don't alter the compute's outcome. Familiar to teams migrating from Prefect's `@task(on_completion=..., on_failure=...)`.

## Cost

**$0.** Fully offline.

## Required env vars

None.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_hooks_asset_demo.sh | bash
cd hooks-asset-demo
uv run dg dev
```

## After the demo — inspect in the UI

```bash
cd hooks-asset-demo
export DAGSTER_HOME=$(pwd)/.dagster_home
uv run dg dev
```

Click each asset → **Logs panel** shows the `[hook.*]` info / error entries from the callbacks. `py_failure` shows STEP_FAILURE with the `notify_failure` log entry preceding the traceback.

## See also

- [`hooks_asset` component reference](https://dagster-component-ui.vercel.app/c/hooks_asset)
- [`sla_asset` walkthrough](sla_asset.md) — pairs naturally: `HooksAssetComponent { on_failure: [page_oncall], wraps: SlaAssetComponent { ... } }` for SLA-breach paging
- [`throttle_asset` walkthrough](throttle_asset.md) — cross-run rate limiting via the same `wraps:` pattern
- Browse the [walkthrough index](README.md) for more decorator + infrastructure demos.
