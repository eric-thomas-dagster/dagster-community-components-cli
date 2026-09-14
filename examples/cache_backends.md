# Task result caching in Dagster — from local FS to remote result storage, with three lines of YAML

**Caching results across runs — local FS for dev, IO-manager-backed for prod (S3 / GCS / anything fsspec supports).**

## The problem

Expensive computations — feature engineering over a big table,
prep work for a model-training run, an aggregation that grinds a
warehouse for a full minute — need to be cached across runs. Not just
"cache within one run" (that's easy) but "cache across runs so
tomorrow's materialization skips the work if the inputs haven't
changed." The pattern people reach for in Prefect looks like this:

```python
@task(cache_key_fn=task_input_hash, persist_result=True,
      result_storage=S3Bucket.load("prod-cache"),
      cache_expiration=timedelta(hours=1))
def build_features(rows): ...
```

Dagster covers the same ground with a slightly different vocabulary.
The `@cached` decorator + the `TaskCache` protocol (from the
`task_asset` component) give you the same three axes — WHERE the
cache lives, WHAT invalidates it, and HOW to force a refresh — with a
YAML-first ergonomics that lands as three-lines-of-config for the
simple case and swaps to a remote backend by pointing at an IO manager.

Two shipping shapes cover the ladder:

- **`CachedAssetComponent`** — the "just wrap an asset and cache to a
  path" tier. `cache_dir` accepts a local path (`/var/dagster/cache/`)
  or an fsspec URI (`s3://bucket/prefix/`, `gs://…`, `abfs://…`).
  Zero glue code; one asset, one directory.
- **`TaskAssetComponent` + `TaskCache`** — the "cache runtime-fanned-out
  sub-tasks by any Dagster IO manager" tier. Wraps `IOManagerBackedTaskCache`
  over any of the 30+ DCC IO managers (S3, GCS, ADLS, DuckDB, Snowflake,
  custom). Anywhere the IO manager can write is a cache backend.

## The 3-tier ladder

|  | Tier | Dagster | Prefect equivalent |
|---|---|---|---|
| 1 | Local FS (dev)         | `@cached(cache_dir="/tmp/cache")`                                 | `@task(persist_result=True, result_storage=LocalFileSystem())` |
| 2 | Local-with-IOM (prod-lite) | `FilesystemTaskCache(base_path="/data/cache")` via `task_asset` | `@task(result_storage=LocalFileSystem(basepath="/data/cache"))` |
| 3 | Remote (prod)          | `IOManagerBackedTaskCache(io_manager_key="s3_pickle_io_manager")` via `task_asset` | `@task(result_storage=S3Bucket(...))` |

Same vocabulary, three lines of YAML each, and the pattern doesn't
change shape as you climb the ladder — you swap the `cache_dir` value
or point at a different `io_manager_key`.

## Tier 1 — local FS with `@cached`, the "three-line" default

The simplest end-to-end. One `defs.yaml`, no Python file needed for
the cache-key function — a stock helper hashes upstream inputs the same
way Prefect's `task_input_hash` does:

```yaml
type: dagster_community_components.CachedAssetComponent
attributes:
  asset_name: expensive_features

  compute:
    kind: python
    python: "my_project.features:build_expensive"

  cache_dir: /var/dagster/cache/features/
  format: parquet
  code_version: v1
  ttl_seconds: 3600
  key_fn: "dagster_community_components:input_hash_cache_key_fn"
```

Callouts on the fields:

- **`key_fn: "dagster_community_components:input_hash_cache_key_fn"`** —
  the stock helper that hashes upstream inputs like Prefect's
  `task_input_hash`. No `my_project/cache_key.py` file to write. If
  you want a custom hash (e.g. content-hash of a DataFrame instead of
  `repr()`) point `key_fn` at your own `module:function`.
- **`ttl_seconds: 3600`** — one-hour freshness. Same as Prefect's
  `cache_expiration=timedelta(hours=1)`. On the next run, if the
  cache file's mtime is older than the TTL, it's treated as a miss
  and the compute re-runs.
- **`code_version: v1`** — mixed into the cache key. Bump it to
  invalidate permanently (until the next bump). Also set as
  `code_version` on the underlying `@dg.asset` so Dagster's built-in
  change detection notices for downstream lineage.
- **`cache_dir`** — accepts local FS OR fsspec URIs. Flip to
  `s3://my-bucket/cache/features/` and the same asset now writes cache
  entries to S3 with zero code change. Perfect for jumping from `dg dev`
  on your laptop straight to Dagster+ Serverless without a code diff.

Cache key composition = `hash(asset_key + code_version + partition_key +
key_fn(context, upstream))`. Truncated to 24 chars, filenamed as
`{cache_dir}/{key}.parquet`. Cache hits load the parquet and skip the
compute; misses run the compute, save the parquet, and emit typed
metadata (`cache_status`, `cache_key`, `cache_path`, `cache_rows`) that
renders in the UI.

## The `refresh_cache=true` run tag — one-run bust without a code_version bump

Prefect's `.submit(refresh_cache=True)` idiom has a direct Dagster
equivalent — a run-tag that forces a MISS for a single run without
editing `code_version`:

```bash
# CLI: force a refresh for this one materialization
uv run dg launch --assets 'expensive_features' --tags refresh_cache=true

# From Python:
dg.materialize([expensive_features], tags={"refresh_cache": "true"})
```

Both `refresh_cache=true` and the fully-qualified
`dagster/refresh_cache=true` are accepted. Dagster+ users can set the
tag from the run launcher form; sensors can request one-off refreshes
via `RunRequest(tags={"refresh_cache": "true"})`.

Under the hood the wrapped asset checks `context.run.tags` on every
materialization; if the flag is set, `_load_cache()` is bypassed and
the log line reads:

    [cached] refresh_cache=true run tag — forced MISS, key=<hash>

The compute runs, the fresh result overwrites the cache file, and the
next non-tagged run picks up the new value. Same key, same path — no
namespace drift.

## Tier 2 + 3 — `TaskCache` for remote result storage via any IO manager

`CachedAssetComponent` handles the "cache the whole asset" case. When
you need to cache runtime-decided sub-work — a fan-out over N URLs, an
agent tool-call loop, per-item enrichment where you want each sub-item
memoized — reach for `TaskAssetComponent` with a `TaskCache` binding.

Two backends ship in the box:

- **`FilesystemTaskCache`** — local disk, pickle files under
  `<base_dir>/<sha256(key)>.pkl`. Optional TTL enforced on `get`.
  Prod-lite when a single node with a mounted volume is enough.
- **`IOManagerBackedTaskCache`** — adapter that wraps ANY Dagster IO
  manager as a task cache backend. If you already have an S3 / GCS /
  ADLS / Snowflake / custom IO manager configured for asset outputs,
  reuse it as the cache store — no separate storage config, no
  per-backend wrapper class.

### Wiring a remote task cache

Two YAML files:

```yaml
# defs/cache_io_manager/defs.yaml
# Wire an S3-backed pickle IO manager as the cache backend.
type: dagster_community_components.S3PickleIOManagerComponent
attributes:
  s3_bucket: my-prod-cache
  s3_prefix: dagster-task-cache/
  io_manager_key: cache_io_manager
```

```yaml
# defs/expensive_features/defs.yaml
# Task-asset with an IO-manager-backed cache.
type: dagster_community_components.TaskAssetComponent
attributes:
  asset_name: expensive_features
  cache:
    kind: io_manager_backed
    io_manager_key: cache_io_manager
  task_fn: "my_project.features:build_features"
```

Callouts:

- **`IOManagerBackedTaskCache` inherits the IO manager's backend
  automatically.** Anywhere the IO manager can write is a cache backend
  — S3, GCS, ADLS, DuckDB, Snowflake, filesystem, custom. Swap
  `S3PickleIOManagerComponent` for `GCSPickleIOManagerComponent` or
  `AzureBlobPickleIOManagerComponent` and the cache silently moves.
- **Compared to Prefect's `result_storage=S3Bucket(...)` block** —
  same net effect, but any DCC IO manager works without a per-backend
  wrapper. There are 30+ in the registry today; every one of them is a
  valid cache backend.
- **Cache scoping is automatic.** `TaskCache` keys are scoped to
  `root_run_id`, so a re-execute-from-failure of a partially-failed
  run picks up cached results from the prior attempt (the resumability
  story) — and a fresh materialization starts with an empty cache
  (no "did last week's cached value bleed into today?" foot-gun).

### Programmatic form (no YAML)

For projects that already have a Python `Definitions`:

```python
from dagster import Definitions, asset, materialize
from dagster_community_components.task_asset import (
    task, task_asset, FilesystemTaskCache, IOManagerBackedTaskCache,
)
from dagster_aws.s3 import s3_pickle_io_manager

CACHE = FilesystemTaskCache(base_dir="/data/cache", ttl_seconds=3600)
# Or, remote:
# CACHE = IOManagerBackedTaskCache(s3_pickle_io_manager, ttl_seconds=3600)

@task(cache=CACHE, cache_key_fn=lambda ctx, url: url, cache_ttl_seconds=3600)
def fetch_url(context, url):
    return httpx.get(url).text

@task_asset
def enriched_urls(context):
    for url in URLS:
        fetch_url(context, url)     # per-URL cache; hits skip the fetch
```

## Compare-and-contrast — Prefect vs Dagster caching

| Feature | Prefect `@task` | Dagster `@cached` / `TaskCache` |
|---|---|---|
| Basic function-as-task                | ✓                                                | ✓                                                                             |
| Retry on failure                      | `retries=N`                                      | Dagster's `RetryPolicy`                                                        |
| Timeout                               | `timeout_seconds=N`                              | `@timeout` companion decorator                                                 |
| Cache with TTL                        | `cache_expiration=timedelta(hours=1)`            | `ttl_seconds=3600`                                                             |
| Cache-key from inputs                 | `cache_key_fn=task_input_hash`                   | `key_fn: "dagster_community_components:input_hash_cache_key_fn"`               |
| Custom cache-key                      | `cache_key_fn=my_fn`                             | `key_fn: "my_project:my_fn"`  (module:callable ref)                            |
| Force one-run refresh                 | `.submit(refresh_cache=True)`                    | `--tags refresh_cache=true`                                                    |
| Result persistence                    | `persist_result=True`                            | Cache file / IO manager (always persistent)                                    |
| Remote result storage                 | `result_storage=S3Bucket(...)`                   | `IOManagerBackedTaskCache(io_manager_key=...)`                                 |
| Log prints                            | `log_prints=True`                                | `@log_prints` companion decorator                                              |
| On-completion / on-failure hooks      | `on_completion=[fn]`                             | `@on_hooks` companion decorator                                                |
| Fan-out (dynamic per-item)            | `.map()`                                         | `task_asset` layers OR `@task` calls inside a `@task_asset`                    |
| Cross-attempt resumability            | Cache survives if key stable                     | `TaskCache` auto-scoped to `root_run_id` → re-execute-from-failure "just works"|
| Cache-hit observability               | Log line                                         | `AssetObservation(tags={cached_asset_status: hit/miss})` + typed metadata      |

Same shape, different vocabulary. Nothing on this ladder requires you
to fork a project or move between abstractions; the fields on
`CachedAssetComponent` and `TaskAssetComponent` line up 1:1 with the
knobs on Prefect's `@task` decorator.

## Live output — three back-to-back runs

Actual log excerpt for the same `expensive_features` asset, run three
times in a row. Run 1 is cold, run 2 hits the cache, run 3 uses the
`refresh_cache=true` tag to force a bust without editing YAML:

```
>>> Run 1  —  cold cache
[cached] MISS for key=8f2c1e93a4b6c0d7e5f8b219 — running compute
[cached] saved 42_113 rows to /var/dagster/cache/features/8f2c1e93a4b6c0d7e5f8b219.parquet
[ASSET]  expensive_features  cache_status=miss  cache_rows=42113  duration=3.14s

>>> Run 2  —  cache warm  (same inputs, same code_version, no refresh tag)
[cached] HIT for key=8f2c1e93a4b6c0d7e5f8b219 at /var/dagster/cache/features/8f2c1e93a4b6c0d7e5f8b219.parquet (42113 rows)
[ASSET]  expensive_features  cache_status=hit  cache_rows=42113  duration=0.08s

>>> Run 3  —  same inputs, but --tags refresh_cache=true
[cached] refresh_cache=true run tag — forced MISS, key=8f2c1e93a4b6c0d7e5f8b219
[cached] saved 42_113 rows to /var/dagster/cache/features/8f2c1e93a4b6c0d7e5f8b219.parquet
[ASSET]  expensive_features  cache_status=miss  cache_rows=42113  duration=3.09s
```

Every one of those log lines has a paired `AssetObservation` in the
event log with `cached_asset_status: hit|miss` as a tag and
`cache_key` / `cache_path` as typed metadata — searchable in the UI,
queryable via the Dagster+ audit-log GraphQL, and easily promoted to
an Insights hit-rate metric.

## LRU eviction — the cap that keeps `cache_dir` from growing forever

Two optional caps on `CachedAssetComponent` / `@cached`, orthogonal to
TTL:

```yaml
type: dagster_community_components.CachedAssetComponent
attributes:
  asset_name: expensive_features
  # ...as before...
  max_entries: 500                # keep 500 most-recent parquets
  max_bytes: 5_000_000_000        # AND stay under 5 GB total
```

After every miss-write, the directory is scanned; oldest-mtime files
are evicted until BOTH caps are satisfied. Log line + AssetObservation
records the eviction so it's auditable in the event log.

v1 caveat — LRU eviction runs on the LOCAL filesystem only. If
`cache_dir` is an `s3://` / `gs://` / `abfs://` URI, eviction is
skipped with a warning; use the cloud provider's bucket lifecycle
rules for now.

## Run — end-to-end in a fresh project

```bash
# Scaffold + install + wire the cached asset
uvx create-dagster@latest project cache-demo --no-uv-sync
cd cache-demo
uv add -q pandas dagster-community-components-cli
uv add --dev -q dagster-dg-cli
uvx --from dagster-community-components-cli dagster-component add cached_asset --auto-install

# Write a tiny compute function
mkdir -p src/cache_demo/features
cat > src/cache_demo/features/__init__.py <<'PY'
import pandas as pd, time

def build_expensive(context):
    time.sleep(3)                                  # pretend this hits a warehouse
    return pd.DataFrame({"id": range(42_113), "score": [i * 0.1 for i in range(42_113)]})
PY

# Wire the component (edit src/cache_demo/defs/cached_asset/defs.yaml
# to match the YAML above — asset_name, compute.python, cache_dir, etc.)

# Materialize three times to see MISS → HIT → forced MISS
uv run dg launch --assets 'expensive_features'                                  # MISS  (3s)
uv run dg launch --assets 'expensive_features'                                  # HIT   (<0.1s)
uv run dg launch --assets 'expensive_features' --tags refresh_cache=true        # MISS  (3s)

# ...or open the UI:
uv run dg dev
```

A `setup_cache_backends_demo.sh` script will ship in `examples/`
alongside this walkthrough so `curl | bash` reproduces the whole thing
in one shot.

## Companion walkthroughs / components

- [`cached_asset` component reference](https://dagster-component-ui.vercel.app/c/cached_asset) — the component this walkthrough anchors on. Full field reference + live examples.
- [`task_asset` component reference](https://dagster-component-ui.vercel.app/c/task_asset) — the runtime-fan-out shape with `TaskCache` bindings.
- [`s3_pickle_io_manager`](https://dagster-component-ui.vercel.app/c/s3_pickle_io_manager) / [`gcs_pickle_io_manager`](https://dagster-component-ui.vercel.app/c/gcs_pickle_io_manager) / [`azure_blob_pickle_io_manager`](https://dagster-component-ui.vercel.app/c/azure_blob_pickle_io_manager) — the drop-in IO managers that back `IOManagerBackedTaskCache`.
- [`agentic_pipeline`](https://dagster-component-ui.vercel.app/c/agentic_pipeline) walkthrough — where `TaskCache` is used for LLM-call memoization inside a `tool_use_loop` (cache the "same tool call → same answer" step within one investigation).
- [`cached_asset.md`](https://dagster-component-ui.vercel.app/examples/cached_asset) — the standalone `@cached` demo (3 back-to-back runs: MISS → HIT → MISS after `code_version` bump).

## See also

- <https://dagster-component-ui.vercel.app/c/cached_asset>
- <https://dagster-component-ui.vercel.app/c/task_asset>
- <https://dagster-component-ui.vercel.app/c/s3_pickle_io_manager>
- Walkthrough index: [examples/README.md](README.md)
