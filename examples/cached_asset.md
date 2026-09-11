# cached_asset — `@cached` decorator + `CachedAssetComponent.wraps:` composability
> ✅ **100% offline** — no API keys, no cloud, no external services.

**Live-validated** — the setup script runs end-to-end demonstrating BOTH
shapes of the DCC decorator API in ~90 seconds.

## What this demo shows

Two assets, one per shape. Both use content-addressable caching under
the hood; both emit `cached_asset_status=hit|miss` observations:

| Run | Asset | Behavior |
|---|---|---|
| 1 | `py_expensive_report` (Python decorator) | MISS — 3s compute, writes parquet cache |
| 2 | `py_expensive_report` (Python decorator) | HIT — loads parquet, skips compute (near-zero) |
| 3 | `yaml_expensive_report` (YAML composability) | MISS — inner `SyntheticDataGeneratorComponent` runs (1000 rows), writes parquet cache |
| 4 | `yaml_expensive_report` (YAML composability) | HIT — inner data-gen **NOT invoked**, parquet loaded |

## The two shapes — same primitive, different authoring surface

**Shape 1: Python decorator** — best when you already have Python code you want to cache:

```python
# src/<pkg>/defs/py_expensive_report.py
import time
import pandas as pd
import dagster as dg
from dagster_community_components import cached

CACHE_DIR = "/tmp/.cache/py_shape"

@dg.asset(code_version="1.0", group_name="python_decorator")
@cached(cache_dir=CACHE_DIR, code_version="1.0", ttl_seconds=3600)
def py_expensive_report(context) -> pd.DataFrame:
    time.sleep(3)
    return pd.DataFrame({"metric": [...], "value": [...]})
```

**Shape 2: YAML composability — the money shot.** `CachedAssetComponent` wraps **another DCC component**. Zero Python for this asset. The cache intercepts the inner's compute — on hit, the inner is entirely skipped; on miss, the inner runs and its returned DataFrame is cached:

```yaml
# src/<pkg>/defs/yaml_expensive_report/defs.yaml
type: dagster_community_components.CachedAssetComponent
attributes:
  cache_dir: /tmp/.cache/yaml_shape
  code_version: v1
  wraps:
    type: dagster_community_components.SyntheticDataGeneratorComponent
    attributes:
      asset_name: yaml_expensive_report
      schema_type: customers
      row_count: 1000
      random_state: 42
```

**One asset is registered** (`yaml_expensive_report`) — no duplication. Stack arbitrarily deep — `BudgetAssetComponent { wraps: CachedAssetComponent { wraps: SnowflakeQueryComponent } }` = `@budget @cached @snowflake` in Python decorator terms.

Any component whose compute returns a `pandas.DataFrame` can be wrapped. That covers most data-shape components (SnowflakeQuery, BigqueryQuery, DataframeFromCsv, SyntheticDataGenerator, LLMPromptExecutor, etc.).

## Why the YAML composability is the money shot

Before `wraps:`, YAML users had to write Python callables and reference them via `compute: {kind: python, python: 'mod:fn'}` to use decorators. That's fine but requires user Python.

With `wraps:`, `CachedAssetComponent` stacks over **any DCC component** with zero user Python:

- `CachedAssetComponent { wraps: SnowflakeQueryComponent }` — cache a warehouse query result
- `CachedAssetComponent { wraps: LLMPromptExecutorComponent }` — cache LLM responses (huge cost win)
- `CachedAssetComponent { wraps: RestApiFetcherComponent }` — cache external API responses
- `CachedAssetComponent { wraps: DataframeFromSqlComponent }` — cache SQL query results
- `CachedAssetComponent { wraps: SyntheticDataGeneratorComponent }` — the demo shape

## Cache invalidation levers

| Lever | How |
|---|---|
| `code_version` bump | Change the version on both `@dg.asset` and `@cached` (or on `CachedAssetComponent.code_version`) |
| `ttl_seconds` expired | `cached(ttl_seconds=3600)` — cache treated as miss when parquet mtime > 1h old |
| Custom `key_fn` | `cached(key_fn="my_module:key_from_config")` — string mixed into the cache key; use to invalidate when external config changes |
| Manual bust | `rm <cache_dir>/<key>.parquet` |

## Cost

**$0.** Fully offline.

## Required env vars

None.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_cached_asset_demo.sh | bash
cd cached-asset-demo
uv run dg dev
```

## Ways to use this in production

**Use `@cached` when:**
- The compute is expensive Python (LLM calls, heavy pandas, external APIs)
- The result fits comfortably in memory as a `pandas.DataFrame`
- Multiple runs would produce the same output for the same inputs (deterministic + slow)

**Use `CachedAssetComponent.wraps` when:**
- You're already using another DCC component and want to add caching without editing it
- You want the cache layer to be declarative (visible in YAML diff)
- You want the cache decision separate from the component's authorship

**Don't use `@cached` for:**
- Results > ~1 GB — a real query cache (materialized views, Iceberg incrementals) scales better
- Streaming assets — the cache assumes a stable compute
- Cross-run memoization where you want ONE cache entry "forever" — use a real Dagster asset with an IO manager

## After the demo — inspect in the UI

```bash
cd cached-asset-demo
export DAGSTER_HOME=$(pwd)/.dagster_home
uv run dg dev
```

Click either asset → **Observations panel** shows `cached_asset_status: hit|miss` + `cache_key` + `cache_path` metadata for every materialization.

## See also

- [`cached_asset` component reference](https://dagster-component-ui.vercel.app/c/cached_asset)
- [`throttle_asset` walkthrough](throttle_asset.md) — same "wraps" pattern, different primitive
- Browse the [walkthrough index](README.md) for more decorator + infrastructure demos.
