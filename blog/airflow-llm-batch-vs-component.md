---
title: "Airflow shipped a new decorator for batch-mode LLM calls. I built the same thing as one ordinary Dagster component instead."
date: 2026-10-07
author: Eric Thomas
description: "Apache Airflow's common.ai provider landed a dedicated LLMBatchOperator + @task.llm_batch decorator for async batch LLM calls on September 21. I built the equivalent capability — OpenaiLlmBatchComponent / AnthropicLlmBatchComponent — as one ordinary component two weeks later, after reading their PR. No new decorator, no new operator class, no new trigger infrastructure. Here's the detailed comparison, and what it took (an afternoon, not a release cycle) to match Airflow's own rigor on a crash-recovery edge case I'd missed."
---

# Airflow shipped a new decorator for batch-mode LLM calls. I built the same thing as one ordinary component.

**Eric Thomas · October 2026**

*This is an independent community project, not an official Dagster Labs comparison — my own components, my own read of Airflow's PR, my own opinions below.*

---

Apache Airflow's `common.ai` provider merged `LLMBatchOperator` / `@task.llm_batch` on September 21 ([PR #72938](https://github.com/apache/airflow/pull/72938)): submit a list of prompts as one OpenAI or Anthropic batch job, defer while it runs, land results as JSONL on object storage. Real engineering, worth taking seriously — and it needed a brand-new operator class, a brand-new decorator, and a new trigger lifecycle to ship, because the existing `@task.llm` abstraction couldn't stretch to cover it.

I read that PR, and two weeks later built [`OpenaiLlmBatchComponent`](https://dagster-component-ui.vercel.app/c/openai_llm_batch) / [`AnthropicLlmBatchComponent`](https://dagster-component-ui.vercel.app/c/anthropic_llm_batch) — the same job, as one ordinary Dagster component. Same async batch APIs, same idempotency guarantee, same "don't make the caller own JSONL construction and polling by hand." No new decorator. No new operator type. No new trigger class to reason about. One component, the same `asset_name` / `upstream_asset_key` shape every other component in this catalog already uses.

That's the actual comparison this post is making — not "who shipped first" (Airflow did, clearly, and that's what prompted this), but "given the same problem, at the same time, what did each architecture actually require to solve it." I read Airflow's PR description line by line before writing any of this, not a changelog summary — and it's worth saying up front what that close a read turned up: one real edge case in my own components that Airflow's authors had already thought about and I hadn't yet. Closing it took an afternoon of focused work on two existing files. Not a new abstraction, not a release cycle, not a design doc. That's the point the rest of this post is actually making.

## The problem both solve

Provider batch APIs (OpenAI, Anthropic) run at roughly half the price of synchronous calls, with a 24-hour completion window. Wiring that up by hand means owning JSONL construction, upload, chunking to provider limits, polling, and result retrieval — plus keeping all of it idempotent across retries and crash-safe against double-paying. Both solved the real version of this problem, not a toy version:

- **A deterministic identity key, not a run/try number**, so a retry re-attaches to the in-flight batch instead of resubmitting. Airflow's is `(dag, task, run, map_index)`; mine is a content hash (`prompts_hash`, sha256 over the sorted `(custom_id, prompt)` pairs) stamped into the asset's own materialization metadata. Redundant runs over unchanged input cost nothing extra, either way.
- **Results never ride the thin state-passing channel.** Airflow had to build a new split — XCom holds a small manifest, the actual JSONL lives in object storage — because a batch can hold 100k items and XCom isn't built for that. This is free on the Dagster side: the parsed results DataFrame *is* the asset's materialized value, which Dagster's own I/O manager already handles for every asset in the catalog. Nothing new to design.

## Why one component beats a new decorator + operator + trigger stack

This is the part worth dwelling on, because it's not specific to batch LLM calls — it's how this entire component catalog gets built.

`@task.llm_batch` exists as a *second* decorator because `@task.llm`'s agent loop, tool execution, HITL approval, and `message_history` genuinely have no meaning for a job that spans hours of submit/poll/fetch. Airflow's authors reasoned through this correctly in the PR — their existing abstraction couldn't flex to cover it, so the fix was to design and ship a new one: a new operator class, a new decorator, and (for the deferred path) new trigger lifecycle semantics, including a documented `on_kill` story that only works on Airflow 3.3+ (their own gotcha: "on 3.0 to 3.2 a killed deferred task's batch keeps running and a clear re-attaches to it").

`OpenaiLlmBatchComponent` didn't need any of that. It's `asset_name`, `upstream_asset_key`, `source`, a `wait_for_completion` flag — the same shape every component here uses, period. The deferred-equivalent path is a plain Dagster sensor polling live batch status and firing a `RunRequest` once terminal. A cancelled or failed batch just fails the results asset (`if batch.status != "completed": raise`) — there's no separate kill-signal lifecycle to get right, because polling-and-checking-status was never a special case that needed one.

**That's the actual flex a Dagster component gives you: need a new capability, write it.** Not "design a new first-class concept in the framework." Edit a method. This repo's own recent history is the proof, not a hypothetical:

- Dynamic agent routing by declared capability (`required_capabilities` on `delegate`) — one new field, one filter, shipped same session.
- Calling any of the ~112 existing deterministic transform components mid-pipeline (`invoke_component`) — one new op function, reusing `AssetsDefinition`'s existing callability. No new plugin API, no registration system.
- An agent that investigates freely across multiple real tools instead of following a fixed script (`tool_use_loop`) — same component, new op, same YAML shape.
- The crash-recovery fix below — a targeted edit to an existing method in two already-shipped components.

None of those needed a new decorator. None needed a new operator class. None needed a design review cycle. That's what "it's just a Python class with `build_defs()`" buys you that a bespoke operator/decorator/trigger framework doesn't.

## Matching Airflow's own bar — in an afternoon

Airflow's PR is explicit about a race condition most batch implementations miss entirely: *"Intent is recorded before the paid submit call, so a crash between 'request sent' and 'response recorded' is recoverable on OpenAI through batch metadata; Anthropic offers no such lookup and falls through to an explicit `on_orphaned_intent` policy."*

Good engineering, and a fair bar to hold my own components to. Checking against it line by line found the identical unhandled race in both of mine: a crash between `batches.create()` succeeding (a paid call) and the asset's materialization recording that batch's id would leave the next run with no record of it — a silent duplicate paid resubmit.

Closed both, to the actual capability of each provider's real API rather than papering over the difference:

- **OpenAI**: already had the hook. `batches.create()` already took a `metadata` field (already used for `prompts_hash`), and `batches.list()` exists. Before any fresh submit, scan recent batches for one already tagged with the current hash and reattach instead of creating a new one — the exact recovery path Airflow's own PR describes for OpenAI.
- **Anthropic**: no metadata field on `batches.create()` at all — confirmed against the installed SDK, not assumed, and it's the identical limitation Airflow's own PR calls out for Anthropic. Same idea, implemented with what Dagster already provides instead of a metadata field: an `AssetObservation` records submission intent immediately before the paid call, and a later run that finds an unresolved intent for the same hash stops with clear recovery instructions by default — or proceeds, if explicitly told to accept the risk (`on_orphaned_intent: resubmit`).

Verified against fake clients simulating a real crash (the fake `create()` succeeds, then raises — "paid call completed, process died before the function returned") before any of it shipped. Two files, one afternoon, zero new infrastructure.

## Where Airflow is ahead, and why that gap hasn't been closed yet

`BatchAdapter`'s pluggable dispatch (by `model_id` prefix, through `register_adapter()` or an entry point) is a real idea, and a real gap here: today there are two separate, hardcoded single-provider components, not one extensible abstraction. Worth being precise about why, rather than waving it away — unifying them was considered. The path actually considered was building on `litellm` (the provider-abstraction library used everywhere else in this catalog), and it was rejected: `litellm` only wraps Anthropic's `retrieve_batch`, with no `create_batch` / `cancel_batch` support for Anthropic at all, so it couldn't actually deliver a real single-component abstraction. That's a specific, documented reason to not build on `litellm` for this — it is not evidence that a from-scratch dispatcher, built the way `BatchAdapter` is, would be hard. It's just a different, unattempted approach, and I'm not going to claim it's "cheap" without having built it. Unlike everything else in this post, this one's an open question, not a demonstrated result.

## Try it

[`openai_llm_batch`](https://dagster-component-ui.vercel.app/c/openai_llm_batch) and [`anthropic_llm_batch`](https://dagster-component-ui.vercel.app/c/anthropic_llm_batch) — full field reference and a working `example.yaml` on each page.
