# log_prints_asset — `@log_prints` decorator + `LogPrintsAssetComponent.wraps:` composability
> ✅ **100% offline** — no API keys, no external services.

**Live-validated** — the setup script runs end-to-end demonstrating BOTH
shapes of the DCC decorator API in ~90 seconds.

## What this demo shows

Two assets, one per shape. Both redirect `sys.stdout` inside the compute
to `context.log.info` line-by-line — every captured line becomes a real
Dagster log event, searchable by `run_id`, visible in the UI's log panel:

| Run | Asset | Behavior |
|---|---|---|
| 1 | `py_ported_script` (Python decorator) | 4 `print()` calls inside compute → 4 `[legacy]`-prefixed log lines |
| 2 | `yaml_customers` (YAML composability) | Inner `SyntheticDataGeneratorComponent` runs cleanly; outer log_prints intercepts stdout (SyntheticDataGen uses `context.log.info` directly so there's nothing on stdout to capture — the wrap is a transparent no-op in that case) |

## The two shapes — same primitive, different authoring surface

**Shape 1: Python decorator** — best when you're porting old scripts that use `print()` for visibility:

```python
# src/<pkg>/defs/py_ported_script.py
import dagster as dg
from dagster_community_components import log_prints

@dg.asset(group_name="python_decorator")
@log_prints(prefix="[legacy] ")
def py_ported_script(context):
    print("hello world")
    print("starting the ported job")
    print(f"processed {123} rows")
    print("done")
    return {"status": "ok"}
```

Every `print(...)` becomes a `context.log.info("[legacy] ...")`. No rewrite required.

**Shape 2: YAML composability — the money shot.** `LogPrintsAssetComponent` wraps **another DCC component**. Any stdout the inner component (or any lib it calls — pandas warnings, requests redirects, tqdm progress bars, `print()` debug statements) writes gets captured line-by-line. Zero Python for this asset:

```yaml
# src/<pkg>/defs/yaml_customers/defs.yaml
type: dagster_community_components.LogPrintsAssetComponent
attributes:
  prefix: "[wrapped] "
  wraps:
    type: dagster_community_components.SyntheticDataGeneratorComponent
    attributes:
      asset_name: yaml_customers
      schema_type: customers
      row_count: 100
      random_state: 42
```

**One asset is registered** (`yaml_customers`) — no duplication. If the inner ever writes to stdout, every line gets captured. If the inner is quiet (as `SyntheticDataGenerator` is — it uses `context.log.info` directly), the wrap is a zero-cost no-op. Inner's config (partitions, deps, kinds, tags, group) passes through unchanged.

Add or remove the outer wrap without touching the inner's config. Stack arbitrarily deep — `LogPrintsAssetComponent { wraps: DockerContainerAssetComponent { ... } }` is a natural fit: the container's stdout streams into the Dagster run log automatically.

## Why the YAML composability is the money shot

Before `wraps:`, YAML users had to write Python callables and reference them via `compute: {kind: python, python: 'mod:fn'}` to use decorators. That's fine but requires user Python.

With `wraps:`, `LogPrintsAssetComponent` stacks over **any DCC component** with zero user Python — especially useful for components that shell out or call third-party libs:

- `LogPrintsAssetComponent { wraps: DockerContainerAssetComponent }` — container stdout streams into Dagster log
- `LogPrintsAssetComponent { wraps: JupyterNotebookAssetComponent }` — notebook `print()` cells captured
- `LogPrintsAssetComponent { wraps: ShellCommandComponent }` — shell script stdout captured
- `LogPrintsAssetComponent { wraps: LLMPromptExecutorComponent }` — any print-based debug from the LLM stack lands in the run log
- `LogPrintsAssetComponent { wraps: SyntheticDataGeneratorComponent }` — the demo shape (no-op since SyntheticDataGen already uses context.log)

## Components used

| Component | What it does |
|---|---|
| `log_prints_asset` (`@log_prints` decorator + `LogPrintsAssetComponent`) | Redirect `sys.stdout` inside compute to `context.log.info` line-by-line. Buffered on `\n`; empty lines skipped; original stdout restored on completion (success OR failure). `prefix` prepended to every captured line. |
| `synthetic_data_generator` | Inner component the YAML shape wraps. |

## Why this belongs in Dagster

- **Porting legacy scripts is free** — no rewrite required. Just decorate.
- **Third-party lib prints don't leak** — pandas warnings, requests redirects, tqdm progress bars all land in the run log instead of the process terminal.
- **Same primitive, different surface** — Python + YAML shapes emit identical `context.log.info` events.
- **Composability at the component layer** — wrap ANY DCC component (especially notebook / container / shell-out ones) with print-capture without editing that component's config.

## Cost

**$0.** Fully offline.

## Required env vars

None.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_log_prints_asset_demo.sh | bash
cd log-prints-asset-demo
uv run dg dev
```

## After the demo — inspect in the UI

```bash
cd log-prints-asset-demo
export DAGSTER_HOME=$(pwd)/.dagster_home
uv run dg dev
```

Click `py_ported_script` → **Logs panel** shows every captured print line with the configured prefix.

## See also

- [`log_prints_asset` component reference](https://dagster-component-ui.vercel.app/c/log_prints_asset)
- [`profile_asset` walkthrough](profile_asset.md) — same `wraps:` pattern, different primitive
- [`hooks_asset` walkthrough](hooks_asset.md) — asset-scoped success/failure callbacks
- Browse the [walkthrough index](README.md) for more decorator + infrastructure demos.
