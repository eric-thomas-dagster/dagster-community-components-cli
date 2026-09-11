# cached_asset — `@cached` decorator: skip expensive Python compute when nothing changed
> ✅ **100% offline** — no API keys, no cloud, no external services.

**Live-validated** — the setup script runs end-to-end and shows one full
cache miss → hit → invalidation cycle in ~90 seconds.

```
   expensive_report  ← @dg.asset + @cached decorator
        │
        └── cache_dir: <project>/.cache/expensive_report/
            ├── c67b5184...parquet   (code_version 1.0 result)
            └── a8d2e3d8...parquet   (code_version 1.1 result — same fn, new key)
```

## What this demo shows

Three back-to-back runs of the same asset, each ending in a different
cache outcome:

| Run | code_version | Outcome | Compute time |
|---|---|---|---|
| 1 | `1.0` | MISS — no parquet at that key yet, run + write | ~3s (sleep 3) + Dagster overhead |
| 2 | `1.0` | HIT — same key, load parquet, skip compute | ~0s (Dagster overhead only) |
| 3 | `1.1` | MISS — code_version bumped → new key → run + write | ~3s again |

## Components used

| Component | What it does |
|---|---|
| `cached_asset` (`@cached` decorator) | Content-addressable cache for `@dg.asset` compute. Cache key = hash(asset_key + code_version + partition_key + optional user key_fn). On hit, skips the wrapped function entirely and loads the cached parquet. Cache lives at `{cache_dir}/{cache_key}.parquet` — local FS or any fsspec URI (`s3://`, `gs://`, `abfs://`). Companion `CachedAssetComponent` YAML wrapper if you prefer YAML. |

## Why this belongs in Dagster

- **Cache key composes with `code_version`** — Dagster's built-in change
  detection: bump the version on the asset, cache invalidates cleanly.
- **Cache hit/miss surfaced as materialization metadata** — every run
  records `cache_status`, `cache_key`, `cache_path`, `cache_rows`. Query
  the event log for a cache-hit-rate dashboard.
- **Works with any executor** — the parquet file is the shared state; no
  in-memory state to coordinate across processes.

## Cost

**$0.** The demo is fully offline — a `time.sleep(3)` stands in for the
expensive compute.

## Required env vars

None.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_cached_asset_demo.sh | bash
cd cached-asset-demo
uv run dg dev
```

The setup script does everything end-to-end — scaffolds a project,
installs DCC, drops a decorated `@dg.asset`, and runs it 3 times with a
`code_version` bump between runs 2 and 3.

## The decorated asset

```python
# src/<pkg>/defs/expensive_report.py
import time
import pandas as pd
import dagster as dg
from dagster_community_components import cached

CACHE_DIR = "<project>/.cache/expensive_report"

@dg.asset(code_version="1.0", group_name="cache_demo")
@cached(cache_dir=CACHE_DIR, code_version="1.0", ttl_seconds=3600)
def expensive_report(context) -> pd.DataFrame:
    context.log.info("[expensive_report] running compute (sleep 3s)")
    time.sleep(3)
    return pd.DataFrame({
        "metric": ["revenue_usd", "order_count", "avg_order_value"],
        "value":  [125_430.75, 4_218, 29.73],
    })
```

**Decoration order matters** — `@cached` goes UNDER `@dg.asset` (the
cache wrapper is the innermost decorator; Dagster wraps the cached
wrapper). Both take a matching `code_version` so a bump on the asset
also bumps the cache key.

## Ways to invalidate the cache

| Lever | How |
|---|---|
| `code_version` bump | Change the version on both `@dg.asset` and `@cached` |
| `ttl_seconds` expired | `cached(ttl_seconds=3600)` — cache treated as miss when parquet mtime > 1h old |
| Custom `key_fn` | `cached(key_fn="my_module:key_from_config")` — returns a string mixed into the key; use this to invalidate when external config or upstream feature-flags change |
| Manual bust | `rm <cache_dir>/<key>.parquet` |

## What this decorator is (and isn't) for

**Use `@cached` when:**
- The compute is expensive Python (LLM calls, complex pandas, heavy IO to third-party APIs)
- The result fits comfortably in memory as a `pandas.DataFrame`
- Multiple runs would produce the same output for the same inputs (deterministic + slow)

**Don't use `@cached` for:**
- Results > ~1 GB — a real query cache (materialized views, Iceberg
  incrementals) will scale better than parquet blobs
- Streaming assets — the cache assumes a stable compute
- Cross-run memoization where you want ONE cache entry to live "forever" —
  use a real Dagster asset with an IO manager instead

## After the demo — inspect in the UI

```bash
cd cached-asset-demo
export DAGSTER_HOME=$(pwd)/.dagster_home
uv run dg dev  # → http://localhost:3000
```

Click `expensive_report` → **Materializations tab**:
- Each run shows `cache_status: hit|miss`, `cache_key`, `cache_path`, `cache_rows`
- Bump `code_version` in the decorator → next run is a MISS
- Roll back to previous `code_version` → hits again (parquet under the old key is untouched)

## See also

- [`cached_asset` component reference](https://dagster-component-ui.vercel.app/c/cached_asset)
- Browse the [walkthrough index](README.md) for more decorator + infrastructure demos.
