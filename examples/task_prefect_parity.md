# Prefect `@task` parity in Dagster — end-to-end runnable demo

A single-command scaffold + materialize that exercises every Prefect `@task` feature the prototype claims parity on. Runs in under a minute, zero external dependencies.

## Try it

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_task_prefect_parity_demo.sh | bash
cd task-prefect-parity-demo
uv run dg dev
```

You'll see one asset — `agentic_ticket_triage` — that exercises every feature in one materialization. The setup script also materializes it once at the end so you see a green run before opening `dg dev`.

## What the demo asset exercises

| `@task` feature | Demo function | Notes |
|---|---|---|
| `cache=True` (auto-hash inputs) | `summarize_ticket` | 3 unique tickets → 3 computes; re-run → 3 HITs |
| `cache_policy=INPUTS + TASK_SOURCE + CROSS_RUN` + `cache_expiration=timedelta(hours=1)` | `classify_intent` | Cross-run cache survives fresh materialization; source-hashed |
| `retry_condition_fn=lambda ctx, exc: isinstance(exc, ConnectionError)` + `max_retries=3` + `retry_delay_seconds=0.05` + `retry_jitter_factor=0.3` | `flaky_api_call` | 40% failure rate; retries `ConnectionError`, would not retry other exceptions |
| `on_awaiting_retry` + `on_completion` hooks | `flaky_api_call` | Fires at real state transitions |
| `timeout_seconds=5` + `log_prints=True` | `bounded_scrape` | Hard-kill past deadline; `print()` redirected to `context.log.info` |
| `concurrency_pool="expensive_llm"` + `max_concurrent=3` | `expensive_llm_call` | 5 calls, only 3 concurrent at any moment |
| `gather_async` — 10 concurrent async `@task` calls | `async_llm_call.aio(...)` + `gather_async(...)` | Real Prefect `asyncio.gather` parity — 10 × 0.15s = ~0.15s concurrent, not 1.5s serial |

## Live run trace (from setup-script materialization)

```
[compute] summarize_ticket(#T-101) — this runs on cache MISS only
[compute] summarize_ticket(#T-102) — this runs on cache MISS only
[compute] summarize_ticket(#T-103) — this runs on cache MISS only
[compute] classify_intent — cache scoped cross-run + source-hashed
[compute] classify_intent — cache scoped cross-run + source-hashed
[compute] classify_intent — cache scoped cross-run + source-hashed
[hook:on_completion] task succeeded
[print] scraping https://blog.example.com...
[print] done in bounded time
gather_async: 10 concurrent async tasks in 0.16s (serial would take ~1.5s)
ASSET_MATERIALIZATION - Materialized value agentic_ticket_triage
RUN_SUCCESS
```

## Prove the cache works

Materialize twice — second run's `summarize_ticket` and `classify_intent` should NOT show the `[compute]` log line (cache HIT):

```bash
uv run dg launch --assets 'agentic_ticket_triage'   # first run — 3 misses each
uv run dg launch --assets 'agentic_ticket_triage'   # second run — cache HIT
```

Force a refresh for a single run without editing code (Prefect `.submit(refresh_cache=True)` parity):

```bash
uv run dg launch --assets 'agentic_ticket_triage' --tags refresh_cache=true
```

## Prove `gather_async` is actually concurrent

Watch the run log — `gather_async: 10 concurrent async tasks in 0.15s` vs `serial would take ~1.5s`. That's a **10× speedup**, not a wrapper.

## What's under the hood

- Single asset: `src/<pkg>/defs/prefect_parity_demo/definitions.py`
- Wired via `dagster.DefinitionsComponent` (`path: definitions.py`) so `dg check` + `dg dev` autoload it
- All imports come from the `task_asset` DCC component installed by the setup script

## Related — the full test suite

Every feature this demo exercises has a corresponding automated test in the templates repo:

- Tests: <https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/tests/prefect_parity>
- Latest run output: <https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/tests/prefect_parity/LATEST_RESULTS.txt>
- `@task` implementation: <https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/assets/infrastructure/task_asset/component.py>

47/47 tests passing as of the most recent capture.
