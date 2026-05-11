# Dynamic Fanout Job demo

Generic `DynamicOut` fan-out — discover a list of items at runtime, process each
in parallel via `.map()`, optionally collect results. The most general
op-job pattern in the registry.

```
@dg.job
  ├─ _discover (DynamicOut)        ← user's discover_callable returns a list
  ├─ _process[item_1] (parallel)
  ├─ _process[item_2] ...           ← user's process_callable runs per item
  └─ _collect                       ← optional aggregator over the list of results
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `dynamic_fanout_job` | infrastructure | DynamicOut + map + collect compound op job |

## Demo callables

The demo writes a small Python file (`fanout_callables.py`) into the project
with three functions:
- `list_urls(category)` → returns 7 fake page-URL dicts
- `fetch_url(item, fake_latency_ms)` → simulates a fetch, returns dict
- `summarize(results)` → aggregates counts / total bytes

```yaml
discover_callable_path: "<pkg>.fanout_callables:list_urls"
discover_kwargs:
  category: news
process_callable_path: "<pkg>.fanout_callables:fetch_url"
collect_callable_path: "<pkg>.fanout_callables:summarize"
mapping_key_field: id   # makes per-item retries stable
```

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_dynamic_fanout_job_demo.sh | bash
cd dynamic-fanout-demo
uv run dg launch --job process_url_list
```

## Expected output

```
discovered 7 item(s)
_process[page_1] processed
_process[page_2] processed
... 7 in parallel
collected 7 result(s) -> {items_processed: 7, total_bytes: 29729, categories: ['news']}
```

## When to use this vs partitions

| Pattern | Use when | Trade-off |
|---|---|---|
| Static partitions | Time-series, dimensions known upfront | Per-partition catalog history |
| Dynamic partitions | Items become known at runtime, want catalog persistence (tenants, customers) | Catalog grows, sensor needed |
| **DynamicOut fan-out** (this) | Items are ephemeral within a run (URLs, files, queue items) | No per-item catalog, just parallel processing |
