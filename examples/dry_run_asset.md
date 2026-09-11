# Safe-mode any asset — cost estimates + diff-vs-current, no writes
> ✅ **100% offline** — no API keys, no external services.

**Live-validated** — the setup script runs end-to-end demonstrating BOTH
shapes of the DCC decorator API in ~2 minutes.

## What this demo shows

Two shapes with a subtle-but-important semantic difference:

| Run | Asset | Behavior |
|---|---|---|
| 1 | `py_costly_write` (Python decorator, `enabled=True`) | Compute RUNS (exercises code path); return value DISCARDED (IO manager NOT invoked); observation tagged `dry_run=true` |
| 2 | `yaml_customers` (YAML composability, `enabled=true`) | Inner compute NEVER INVOKED (no "Generated DataFrame" line); wrap emits synthetic MaterializeResult with `inner_compute_invoked=false` |
| 3 | `yaml_customers_live` (YAML composability, `enabled=false`) | Passthrough — inner runs unchanged (see "Generated DataFrame" line); no dry-run tag |

## The two shapes — same primitive, different strength

**Shape 1: Python decorator** — best when you already have Python code you want to gate:

```python
# src/<pkg>/defs/py_costly_write.py
import pandas as pd
import dagster as dg
from dagster_community_components import dry_run

@dg.asset(group_name="python_decorator")
@dry_run(enabled=True)  # or omit + trigger via run tag / env var
def py_costly_write(context):
    df = build_costly_dataframe()
    return df   # returned value DISCARDED when dry_run enabled
```

Compute **runs** (exercises retries, timing, logging). Return value is **discarded** (IO manager not invoked). Side-effects inside the compute (a stray file write, a POST to an API) still happen — the author must gate those explicitly via `context.run.tags.get("dry_run")`.

**Shape 2: YAML composability — the money shot.** `DryRunAssetComponent` wraps **another DCC component**. When `enabled=true`, the OUTER short-circuits BEFORE calling the inner's compute — the inner **never runs**:

```yaml
# src/<pkg>/defs/yaml_customers/defs.yaml
type: dagster_community_components.DryRunAssetComponent
attributes:
  enabled: true    # ← inner data-gen SKIPPED entirely
  wraps:
    type: dagster_community_components.SyntheticDataGeneratorComponent
    attributes:
      asset_name: yaml_customers
      schema_type: customers
      row_count: 1000
      random_state: 42
```

This is a **stronger dry-run** than the Python decorator — the inner component's side-effects are prevented entirely. Zero risk of a stray API call / file write leaking out. Direct analog of Python `@dry_run @dg.asset` idiom at the component layer, with the added guarantee that the inner is not entered.

**Enable modes** (priority order, highest wins):

1. Explicit `dry_run(enabled=True)` decorator arg / `DryRunAssetComponent.enabled: true` YAML field
2. Run tag `dry_run` in `("true", "1", "yes")`
3. Env var `DAGSTER_DRY_RUN` in `("true", "1", "yes")`
4. Default: disabled (compute + persist normally)

So the same asset can be flipped into dry-run for one run without editing code or redeploying: `dagster-cloud job launch --config '{tags: {dry_run: "true"}}'` or `DAGSTER_DRY_RUN=1 uv run dg launch ...`.

## Why the YAML composability is the money shot

Before `wraps:`, YAML users had to write Python callables and reference them via `compute: {kind: python, python: 'mod:fn'}` to use decorators. That's fine but requires user Python.

With `wraps:`, `DryRunAssetComponent` stacks over **any DCC component** with zero user Python — and gives you the strong "inner NEVER runs" guarantee:

- `DryRunAssetComponent { wraps: DataframeToBigqueryTableComponent }` — dry-run BigQuery writes safely
- `DryRunAssetComponent { wraps: StripeIngestion }` — dry-run external API calls (no requests made)
- `DryRunAssetComponent { wraps: LlmPromptExecutorComponent }` — dry-run LLM calls (no tokens spent)
- `DryRunAssetComponent { wraps: DatabaseReplicationComponent }` — pipeline-plumbing verification without touching source or target
- `DryRunAssetComponent { wraps: SyntheticDataGeneratorComponent }` — the demo shape

## Components used

| Component | What it does |
|---|---|
| `dry_run_asset` (`@dry_run` decorator + `DryRunAssetComponent`) | Skip the write on dry-run mode. Python decorator: compute runs, return value discarded, IO manager not invoked. YAML wrap: inner compute NOT invoked (stronger). Both emit `AssetObservation` tagged `dry_run=true` for audits. |
| `synthetic_data_generator` | Inner component the YAML shape wraps. |

## Why this belongs in Dagster

- **Enable via run tag** — flip any asset into dry-run without editing code or redeploying.
- **Auditable** — every dry run leaves an `AssetObservation` tagged `dry_run=true`, plus `elapsed_seconds` and (when computable) `would_produce_bytes`. Cost audits and post-mortems can filter cleanly.
- **YAML wrap is stronger** — the inner component's side-effects are prevented entirely. Not just "IO manager skipped."
- **Composable** — pair with `@sla` (measure duration on the dry run), `@throttle` (dry runs still respect the throttle window), `@smart_retry` (retries within a dry run).

## Cost

**$0.** Fully offline.

## Required env vars

None. (Optional: `DAGSTER_DRY_RUN=1` toggles the default-disabled shapes.)

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_dry_run_asset_demo.sh | bash
cd dry-run-asset-demo
uv run dg dev
```

## After the demo — inspect in the UI

```bash
cd dry-run-asset-demo
export DAGSTER_HOME=$(pwd)/.dagster_home
uv run dg dev
```

Click each asset → **Materialization panel** shows `dry_run: true|false` + `inner_compute_invoked: false|true` (YAML wrap only) + `elapsed_seconds`. **Observations panel** shows the audit trail — filter by `dry_run=true` tag for a clean cost / activity report.

## See also

- [`dry_run_asset` component reference](https://dagster-component-ui.vercel.app/c/dry_run_asset)
- [`throttle_asset` walkthrough](throttle_asset.md) — same `wraps:` pattern, different primitive (rate-limiting)
- [`cached_asset` walkthrough](cached_asset.md) — cache the result of an expensive compute
- Browse the [walkthrough index](README.md) for more decorator + infrastructure demos.
