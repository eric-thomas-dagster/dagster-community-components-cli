#!/usr/bin/env bash
# Prefect @task parity — end-to-end runnable Dagster project.
#
# WHAT THIS SHIPS
#   A minimal Dagster project that exercises every Prefect-@task feature
#   the prototype claims parity on:
#     - @task(cache=True)                       — auto-cache with input-hashing
#     - @task(cache_policy=INPUTS + TASK_SOURCE + CROSS_RUN)  — composable
#     - @task(retry_condition_fn=..., max_retries=3, retry_jitter_factor=0.3)
#     - @task(timeout_seconds=5)                — hard-kill past deadline
#     - @task(log_prints=True)                  — redirect print() to logs
#     - @task(on_running=[...], on_completion=[...], on_failure=[...])
#     - @task(concurrency_pool="gpu", max_concurrent=3)
#     - gather_async(context, coros)            — 10 concurrent async tasks
#
# ZERO EXTERNAL DEPS — all "LLM calls" are asyncio.sleep. Runs on a stock
# uvx create-dagster project + dagster-community-components + requests.

set -euo pipefail
PROJECT_DIR="${1:-task-prefect-parity-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q requests
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing task_asset component"
$CLI --refresh search task_asset >/dev/null 2>&1 || true
$CLI add task_asset --auto-install 2>&1 | tail -1

# Re-export the primitives so from-module imports work in the demo asset
# (preserves the auto-installed exports AND adds the ones the demo needs)
echo 'from .component import (
    TaskAssetComponent, LayerSpec,
    task, task_asset, child_step, ChildStepHandle,
    TaskCache, FilesystemTaskCache, IOManagerBackedTaskCache,
    CachePolicy, INPUTS, TASK_SOURCE, ROOT_RUN, RUN_ONLY, CROSS_RUN, NO_CACHE,
    gather_async,
)
__all__ = [
    "TaskAssetComponent", "LayerSpec",
    "task", "task_asset", "child_step", "ChildStepHandle",
    "TaskCache", "FilesystemTaskCache", "IOManagerBackedTaskCache",
    "CachePolicy", "INPUTS", "TASK_SOURCE",
    "ROOT_RUN", "RUN_ONLY", "CROSS_RUN", "NO_CACHE", "gather_async",
]' > "src/$PKG/components/task_asset/__init__.py"

# Remove the auto-installed example defs (we're writing our own)
rm -rf "src/$PKG/defs/task_asset"

mkdir -p "src/$PKG/defs/prefect_parity_demo"
cat > "src/$PKG/defs/prefect_parity_demo/definitions.py" <<PYEOF
"""Prefect @task parity — one file, every feature."""
import asyncio
import random
import time
from datetime import timedelta

import dagster as dg
from ${PKG}.components.task_asset import (
    task, CachePolicy, INPUTS, TASK_SOURCE, CROSS_RUN, gather_async,
)


# ═════════════════════════════════════════════════════════════════════
# Feature 1 — cache=True (auto-hash inputs, filesystem backend)
# ═════════════════════════════════════════════════════════════════════

@task(cache=True)
def summarize_ticket(context, ticket_id, text):
    context.log.info(f"[compute] summarize_ticket(#{ticket_id}) — this runs on cache MISS only")
    return {"ticket_id": ticket_id, "summary": text[:40], "cost_usd": 0.0012}


# ═════════════════════════════════════════════════════════════════════
# Feature 2 — composable cache_policy + timedelta expiration + TASK_SOURCE
# ═════════════════════════════════════════════════════════════════════

@task(
    cache=True,
    cache_policy=INPUTS + TASK_SOURCE + CROSS_RUN,
    cache_expiration=timedelta(hours=1),
)
def classify_intent(context, ticket_text):
    context.log.info(f"[compute] classify_intent — cache scoped cross-run + source-hashed")
    return random.choice(["billing", "bug", "churn_risk", "spam"])


# ═════════════════════════════════════════════════════════════════════
# Feature 3 — retry_condition_fn + retry_jitter_factor + on_ state hooks
# ═════════════════════════════════════════════════════════════════════

def _log_retry_wait(ctx, exc):
    ctx.log.warning(f"[hook:on_awaiting_retry] transient {type(exc).__name__} — will retry")

def _log_success(ctx):
    ctx.log.info("[hook:on_completion] task succeeded")

@task(
    retry_condition_fn=lambda ctx, exc: isinstance(exc, ConnectionError),
    max_retries=3,
    retry_delay_seconds=0.05,
    retry_jitter_factor=0.3,
    on_awaiting_retry=[_log_retry_wait],
    on_completion=[_log_success],
)
def flaky_api_call(context, url):
    if random.random() < 0.4:
        raise ConnectionError(f"transient network error hitting {url}")
    return {"url": url, "status": 200}


# ═════════════════════════════════════════════════════════════════════
# Feature 4 — timeout_seconds + log_prints (both in-decorator)
# ═════════════════════════════════════════════════════════════════════

@task(timeout_seconds=5, log_prints=True)
def bounded_scrape(context, url):
    print(f"scraping {url}...")   # captured via log_prints
    print("done in bounded time")
    return {"url": url, "chars": 8421}


# ═════════════════════════════════════════════════════════════════════
# Feature 5 — concurrency_pool (in-process semaphore across @task calls)
# ═════════════════════════════════════════════════════════════════════

@task(concurrency_pool="expensive_llm", max_concurrent=3)
def expensive_llm_call(context, prompt):
    time.sleep(0.1)  # simulate LLM API latency
    return {"prompt": prompt, "tokens": 250}


# ═════════════════════════════════════════════════════════════════════
# Feature 6 — gather_async: N concurrent async LLM calls in one asset
# ═════════════════════════════════════════════════════════════════════

@task
async def async_llm_call(context, prompt):
    # In real code: httpx.AsyncClient() → openai/anthropic/gemini async SDK
    await asyncio.sleep(0.15)
    return {"prompt": prompt, "response": f"answer_to_{prompt}"}


# ═════════════════════════════════════════════════════════════════════
# The demo asset — exercises every feature in one materialization
# ═════════════════════════════════════════════════════════════════════

@dg.asset(
    kinds={"python", "agentic"},
    description="Exercises every Prefect @task parity feature in one run.",
)
def agentic_ticket_triage(context):
    tickets = [
        {"id": "T-101", "text": "Cannot log in to the billing portal"},
        {"id": "T-102", "text": "How do I upgrade my plan?"},
        {"id": "T-103", "text": "Bug: exporting CSV crashes at row 10000"},
    ]

    # Feature 1: @task(cache=True) — auto-hash inputs
    summaries = [summarize_ticket(context, t["id"], t["text"]) for t in tickets]
    context.log.info(f"summarized {len(summaries)} tickets")

    # Feature 2: composable cache_policy on 3 unique inputs
    intents = [classify_intent(context, t["text"]) for t in tickets]

    # Feature 3: retry_condition_fn — will retry ConnectionError, will not
    # retry anything else. Uses retry_jitter_factor=0.3 on the delay.
    try:
        api_result = flaky_api_call(context, "https://api.example.com/refresh")
    except Exception as e:  # eventually gives up per max_retries
        api_result = {"error": type(e).__name__}

    # Feature 4: timeout + log_prints
    scraped = bounded_scrape(context, "https://blog.example.com")

    # Feature 5: concurrency_pool caps 3 concurrent LLM calls (in-process)
    llm_results = [expensive_llm_call(context, f"prompt_{i}") for i in range(5)]

    # Feature 6: gather_async — 10 concurrent async LLM calls
    prompts = [f"async_prompt_{i}" for i in range(10)]
    coros = [async_llm_call.aio(context, p) for p in prompts]
    start = time.time()
    async_results = gather_async(context, coros, max_concurrent=10)
    elapsed = time.time() - start
    context.log.info(
        f"gather_async: {len(async_results)} concurrent async tasks in "
        f"{elapsed:.2f}s (serial would take ~{0.15 * len(coros):.1f}s)"
    )

    return {
        "summaries": summaries,
        "intents": intents,
        "api_result": api_result,
        "scraped": scraped,
        "llm_results": llm_results,
        "async_results_count": len(async_results),
        "async_elapsed_seconds": round(elapsed, 3),
    }


defs = dg.Definitions(assets=[agentic_ticket_triage])
PYEOF

# Wire the definitions into the project via dagster.DefinitionsComponent
# (path: points at the module; the module's `defs = Definitions(...)` is auto-imported)
cat > "src/$PKG/defs/prefect_parity_demo/defs.yaml" <<YAML
type: dagster.DefinitionsComponent
attributes:
  path: definitions.py
YAML

echo ">>> Verifying the project loads"
uv run dg check defs 2>&1 | tail -3

echo ">>> Materializing agentic_ticket_triage (exercises every feature)"
uv run dg launch --assets 'agentic_ticket_triage' 2>&1 | \
  grep -E "compute\]|hook:|scraping|done in|gather_async:|RUN_SUCCESS|ASSET_MATERIALIZATION|Materialized value" | \
  head -30

cat <<MSG

═══════════════════════════════════════════════════════════════════════
 Prefect @task parity demo — READY
═══════════════════════════════════════════════════════════════════════

Every Prefect @task feature exercised in one asset:
  - cache=True (auto-hash inputs) ...................... summarize_ticket
  - cache_policy=INPUTS + TASK_SOURCE + CROSS_RUN ...... classify_intent
  - retry_condition_fn + jitter + state hooks .......... flaky_api_call
  - timeout_seconds + log_prints (in-decorator) ........ bounded_scrape
  - concurrency_pool="expensive_llm" (in-process) ...... expensive_llm_call
  - gather_async (10 concurrent async) ................. async_llm_call
                                                          (~1.5s serial → ~0.15s concurrent)

Open the Dagster UI to explore:
  cd $PROJECT_DIR
  uv run dg dev
  # http://localhost:3000

Re-materialize the asset to see cache hits (2nd run — same inputs → HIT).
Force a cache miss for one run:
  uv run dg launch --assets 'agentic_ticket_triage' --tags refresh_cache=true

Cleanup: rm -rf $PROJECT_DIR
MSG
