# Dynamic sub-steps — Prefect's `@task` for Dagster (with cache-on-retry)

> ✅ **100% offline** — no API keys, no external services.

**Live-validated** — the setup script runs end-to-end demonstrating all
four shapes of the DCC `task_asset` family in ~3 minutes.

Unique in the DCC decorator lineup: most decorator components ship two
shapes (Python + YAML). This one ships **four**, because runtime-declared
sub-steps show up in four distinct user situations — and each shape trades
graph render vs. branching vs. authoring surface differently.

## The four shapes at a glance

| | `@task` alone | `@task_asset` | `TaskAssetComponent` (YAML) | `child_step` (context manager) |
|---|---|---|---|---|
| Discover work at runtime | ✅ | ✅ | ✅ | ✅ |
| Branch on real task results | ✅ | ❌ (calls return `None` at record time) | ❌ | ✅ |
| Arbitrary nesting depth | ✅ | 1 layer | N compile-time-known layers | ✅ |
| Graph shows individual calls as nodes | ❌ (log tab only) | ✅ | ✅ | ❌ (log tab only) |
| Parallel execution | ❌ (sequential) | ✅ | ✅ | ❌ (sequential) |
| Runtime state / durations queryable | ✅ | ✅ | ✅ | ✅ |
| Result caching (survives failure retry) | ✅ | via `@task` inside | ❌ | ❌ |

Pick per use case. Everything composes freely.

## SHAPE 1 — `@task` alone (log attribution, arbitrary nesting)

Best for **agentic tool-use loops, API pagination, recursive parsers** —
anywhere branching on a `@task` result matters more than seeing individual
calls in the graph tab. The log tab shows the whole nested tree with real
per-call durations + status:

```python
# src/<pkg>/defs/py_task_only/asset.py
import time
import dagster as dg
from dagster_community_components import task


@task
def parse_url(context, url):
    context.log.info(f"  [parse_url] fetching {url}")
    time.sleep(0.05)
    return {"url": url, "chars": len(url) * 10}


@task
def parse_text(context, block):
    context.log.info(f"[parse_text] block index={block['idx']}")
    urls = [f"https://example.com/{block['idx']}/a",
            f"https://example.com/{block['idx']}/b"]
    for j, url in enumerate(urls):
        parse_url(context, url, task_name=f"url_{block['idx']}_{j}")  # nested
    return len(urls)


@dg.asset(group_name="py_task_only")
def parse_document_logs(context) -> dict:
    doc_blocks = [{"idx": i, "text": f"block {i}"} for i in range(3)]
    for block in doc_blocks:
        parse_text(context, block, task_name=f"text_{block['idx']}")
    return {"n_blocks": len(doc_blocks)}
```

Log tab after one materialization:

```
[task:parse_text[text_0]] → start (step_key=parse_document_logs.parse_text[text_0])
[task:parse_url[url_0_0]] → start (step_key=parse_document_logs.parse_text[text_0].parse_url[url_0_0])
[task:parse_url[url_0_0]] ← ok in 58.7ms
[task:parse_url[url_0_1]] → start (step_key=parse_document_logs.parse_text[text_0].parse_url[url_0_1])
[task:parse_url[url_0_1]] ← ok in 62.8ms
[task:parse_text[text_0]] ← ok in 180.8ms
[task:parse_text[text_1]] → start ...  (...and so on for text_1, text_2)
```

Nine synthetic `STEP_START` / `STEP_SUCCESS` events, all under one
`parse_document_logs` graph node. Because the calls execute imperatively
you can `if parse_text(...)` and branch — but the graph tab doesn't show
per-call nodes.

## SHAPE 2 — `@task_asset` (imperative + real graph fan-out)

Best for **doc-parser / per-item LLM / any "scan then dispatch"** where
you want to see each work item as a first-class graph node. Same
imperative body as SHAPE 1 but every `@task` call inside the `@task_asset`
body is RECORDED (not executed) — after the body finishes the framework
fans out via `DynamicOutput`:

```python
# src/<pkg>/defs/py_task_asset/asset.py
from dagster_community_components import task, task_asset


@task
def parse_url_g(context, url): ...


@task
def parse_text_g(context, block): ...


@task_asset(group_name="py_task_asset",
            description="Every @task call = 1 graph node under run_task[?]")
def parse_document_graph(context):
    doc_blocks = [{"idx": i, "text": f"block {i}"} for i in range(3)]
    for block in doc_blocks:
        parse_text_g(context, block, task_name=f"text_{block['idx']}")
        for j in range(2):
            url = f"https://example.com/{block['idx']}/{chr(ord('a') + j)}"
            parse_url_g(context, url, task_name=f"url_{block['idx']}_{j}")
```

Graph tab shows: `parse_document_graph_scan → run_task[text_0]`,
`run_task[text_1]`, `run_task[text_2]`, `run_task[url_0_0]`,
`run_task[url_0_1]`, ..., `run_task[url_2_1]` (9 fan-out nodes) `→ parse_document_graph_collect`.

Every fan-out is a real Dagster step with its own STEP_SUCCESS, its own
worker slot, parallel execution.

**Constraint**: `@task` calls inside `@task_asset` return `None` at
record time — you CAN'T branch on the return value. If you need
branching, use SHAPE 1.

## SHAPE 3 — `TaskAssetComponent` YAML (layered pipelines)

Best for **N-hop pipelines where each hop's fan-out width comes from
data**. Declare N compile-time-known layers in YAML; the number of
items each layer emits is 100% runtime-discovered:

```yaml
# src/<pkg>/defs/yaml_layered/defs.yaml
type: dagster_community_components.TaskAssetComponent
attributes:
  asset_name: parse_document_yaml
  layers:
    - name: scan_documents
      compute: "<pkg>.computes.layers:scan_documents"
    - name: parse_url
      compute: "<pkg>.computes.layers:parse_url"
  terminal: "<pkg>.computes.layers:summarize"
  group_name: yaml_layered
  kinds: [python, task, custom-parser]
```

With user layer callables in a plain Python module:

```python
# src/<pkg>/computes/layers.py
def scan_documents(context):
    """MUST return a list of (task_name, task_spec) tuples."""
    items = []
    for doc_id in ("doc_a", "doc_b", "doc_c"):
        for block_idx in range(2):
            items.append(
                (f"{doc_id}_block_{block_idx}",
                 {"doc_id": doc_id, "block_idx": block_idx})
            )
    return items    # 6 items — runtime-discovered fan-out width


def parse_url(context, block_spec):
    return {"doc_id": block_spec["doc_id"], "chars": 42}


def summarize(context, results):
    return {"n_results": len([r for r in results if isinstance(r, dict)])}
```

Graph shape: `scan_documents_scan → parse_url_process[?] → terminal_reduce`.
`parse_url_process[?]` fans out at runtime into one node per scan item.

**Scan output shape**: the first layer MUST return a `list` or `tuple` of
`(task_name, task_spec)` pairs. Generators (`yield`) get silently dropped
by the fan-out iterator — return a list.

## BONUS SHAPE — `@task` + `FilesystemTaskCache` (resumable via retry)

Best for **expensive per-item work that MUST survive one failure**. Pass
`cache=` + `cache_key_fn=` to `@task` and matching keys become cache
hits. Cache keys are auto-scoped to the run's `root_run_id`, so:

- **Same failed run → resumed via "Re-execute from failure"** — succeeded
  `@task` calls from the prior attempt are cache hits; the failed step
  re-runs.
- **Net-new materialization** — fresh `root_run_id`, cache starts empty
  (no bleed from any prior run — deliberate).

```python
# src/<pkg>/defs/py_task_cached/asset.py
import time
import dagster as dg
from dagster_community_components import task, FilesystemTaskCache

_CACHE = FilesystemTaskCache(base_dir="/tmp/task_cache", ttl_seconds=3600)


@task(cache=_CACHE, cache_key_fn=lambda ctx, url: f"parse_url_c:{url}")
def parse_url_c(context, url):
    context.log.info(f"  [parse_url_c] fetching {url} (expensive)")
    time.sleep(0.10)                # simulate an expensive scrape
    return {"url": url, "chars": len(url) * 10}


@dg.asset(group_name="py_task_cached")
def parse_document_cached(context) -> dict:
    urls = [f"https://example.com/doc_{i}/{chr(ord('a') + j)}"
            for i in range(3) for j in range(2)]
    for i, url in enumerate(urls):
        parse_url_c(context, url, task_name=f"url_{i}")
    return {"n_urls": len(urls)}
```

**Demoing the cache without the UI:** the setup script materializes this
asset twice back-to-back. Both materializations are NET-NEW so both MISS
— that's by design (net-new never hits the prior run's cache; use a real
Dagster asset for cross-run memoization).

To see real cache hits, use the UI:

1. In `parse_url_c`, add `if url.endswith("2/b"): raise RuntimeError("boom")`.
2. `uv run dg dev` → materialize `parse_document_cached` → first 5 URLs
   succeed and cache; 6th fails → run status = FAILED.
3. In the runs page, click **Re-execute → From failure**.
4. Dagster preserves `root_run_id` across the retry. The 5 already-cached
   URLs log `[cache_hit] key=parse_url_c:...` (near-zero duration); the
   6th re-runs.

Cache backends beyond filesystem:

- `IOManagerBackedTaskCache(io_manager, ttl_seconds=...)` — wrap any
  Dagster IOManager as the cache backend. Reuse an existing s3_pickle /
  gcs_pickle IO manager without provisioning separate cache storage.
- Custom — implement `TaskCache.get(key) → value | TaskCache.MISS` +
  `put(key, value)`.

## Why this belongs in Dagster

- **Runtime-discovered work + first-class graph** — 4 shapes let you pick
  the tradeoff (arbitrary nesting vs graph render vs branching vs YAML
  authoring) without reaching for another orchestrator.
- **Same primitive under the hood** — `@task_asset` = `@task` +
  `DynamicOutput`; `TaskAssetComponent` = declarative multi-layer
  `DynamicOutput` graph; `child_step` = the raw context manager `@task`
  desugars to. Skills transfer freely.
- **Retry-resumable caching** — the `root_run_id`-scoped cache turns
  Dagster's built-in "Re-execute from failure" into a resume-from-cache
  flow for dynamic workloads. No custom checkpoint logic.
- **Composes with the rest of the DCC decorator family** — wrap the
  outer asset in `@sla` / `@timeout` / `@budget` / `@smart_retry` /
  `@snapshot` to enforce SLAs / hard deadlines / cost caps / retry
  classification / persistent snapshots on the whole thing.

## Components used

| Component | What it does |
|---|---|
| `task_asset` (`@task` + `@task_asset` + `TaskAssetComponent` + `child_step` + `FilesystemTaskCache`) | Dynamic, runtime-declared sub-steps for Dagster assets. Four shapes covering log attribution, graph fan-out, YAML-declared layers, and retry-resumable caching. |

## Cost

**$0.** Fully offline.

## Required env vars

None.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_task_asset_demo.sh | bash
cd task-asset-demo
uv run dg dev
```

Browse in the UI:

- `parse_document_logs` → runs → run detail → logs → nested step_keys
- `parse_document_graph` → graph tab → `run_task[?]` fan-out (9 nodes)
- `parse_document_yaml` → graph tab → `scan → parse_url_process[?] → terminal_reduce`
- `parse_document_cached` → follow the failure-retry recipe above to see `[cache_hit]` lines

## Pair with a sensor

Watch for slow `@task` calls (log-attribution only — `@task` synthetic
events land in the event log too):

```python
@dg.sensor(name="slow_task_watcher")
def slow_task_watcher(context):
    from dagster import DagsterEventType
    for r in context.instance.get_event_records(
        event_records_filter=dg.EventRecordsFilter(event_type=DagsterEventType.STEP_SUCCESS),
        limit=200, ascending=False,
    ):
        dur = getattr(r.dagster_event.event_specific_data, "duration_ms", None) or 0
        key = r.dagster_event.step_key or ""
        if "[task." in key or "task:" in key:   # only synthetic task events
            if dur > 5000:  # ping oncall if any @task takes >5s
                ...
```

## See also

- [`task_asset` component reference](https://dagster-component-ui.vercel.app/c/task_asset)
- [`cached_asset` walkthrough](cached_asset.md) — asset-level cross-run cache; different job from `@task`'s per-sub-call retry-resumption cache.
- [`smart_retry` walkthrough](smart_retry.md) — retry classification for `@task`-wrapped calls.
- [`sla_asset` walkthrough](sla_asset.md) / [`timeout_asset` walkthrough](timeout_asset.md) — enforce whole-asset deadlines around a task pipeline.
- Browse the [walkthrough index](README.md) for more decorator + infrastructure demos.
