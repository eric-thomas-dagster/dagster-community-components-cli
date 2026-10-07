---
title: "Airflow just shipped batch-mode LLM calls. Here's how the community component compares — and a real gap it found in ours."
date: 2026-10-07
author: Eric Thomas
description: "Apache Airflow's new LLMBatchOperator solves the same problem OpenaiLlmBatchComponent/AnthropicLlmBatchComponent do: cheap async batch LLM calls without owning JSONL/polling/retry logic by hand. A fair, detailed comparison — including a real crash-recovery gap the comparison found in our own components, and the fix."
---

# Airflow just shipped batch-mode LLM calls

**Eric Thomas · October 2026**

---

Apache Airflow's `common.ai` provider just landed `LLMBatchOperator` / `@task.llm_batch` ([PR #72938](https://github.com/apache/airflow/pull/72938), authored by [Lee-W](https://github.com/Lee-W)): submit a list of prompts as one OpenAI or Anthropic batch job, defer while it runs, land results as JSONL on object storage. It's a genuinely well-engineered piece of work, and this repo has shipped the same idea for a while now — [`OpenaiLlmBatchComponent`](https://dagster-component-ui.vercel.app/c/openai_llm_batch) / [`AnthropicLlmBatchComponent`](https://dagster-component-ui.vercel.app/c/anthropic_llm_batch), one component each, submit + status-sensor + results bundled together.

I read the actual PR description, not a changelog summary, before writing any of this. Worth doing the comparison properly rather than just asserting we're better — and worth saying up front: researching it found a real gap in our own components that Airflow's PR explicitly solves and ours didn't. Fixed before publishing this. Details below.

## The problem both solve

Provider batch APIs (OpenAI, Anthropic) run at roughly half the price of synchronous calls, with a 24-hour completion window. The catch, in both Airflow's and our words: whoever wires this up by hand owns JSONL construction, upload, chunking to provider limits, polling, and result retrieval. Multiply by "and now keep it idempotent across retries" and "and don't double-pay on a crash" and it's a real amount of undifferentiated plumbing for what's conceptually "send these prompts, get these answers, cheaper."

Both projects landed on the same two non-negotiables:

- **A deterministic identity key**, not a run/try number, so a retry re-attaches to the in-flight batch instead of resubmitting. Airflow's is `(dag, task, run, map_index)`; ours is a content hash (`prompts_hash`, sha256 over the sorted `(custom_id, prompt)` pairs) stamped into the asset's own materialization metadata. Different mechanism, same goal: redundant runs over unchanged input cost nothing extra.
- **Results never ride the thin state-passing channel.** Airflow's XCom only holds a small manifest (counts, result URI, provenance) because a batch can hold 100k items; the actual JSONL lives in object storage. We get this for free: the parsed results DataFrame *is* the asset's materialized value, which Dagster's own I/O manager already handles for every asset in the catalog — it was never a special case to design around.

## Where the shapes genuinely differ

**One component, not a new operator vocabulary.** `@task.llm_batch` exists because `@task.llm`'s agent loop, tool execution, HITL approval, and `message_history` have no meaning for a job that spans hours of submit/poll/fetch — a fair design call, explicitly reasoned through in the PR. The tradeoff is a second decorator with its own semantics to learn. `OpenaiLlmBatchComponent` doesn't need that split: it's the same `asset_name` / `upstream_asset_key` / `source` shape every component in this catalog uses, a `wait_for_completion` flag swapping between blocking and deferred-equivalent modes. No new mental model for "this one's a batch job."

**No separate trigger infrastructure.** Airflow's deferred mode needs a triggerer process and, as of Airflow 3.3, a documented `on_kill`-handling story for what happens if a UI clear fires mid-flight (earlier versions: "a killed deferred task's batch keeps running and a clear re-attaches to it" — their own gotcha, not mine). Our deferred-equivalent is a plain Dagster sensor polling live batch status and firing a `RunRequest` once terminal. A cancelled or failed batch simply fails the results asset rather than parsing it (`if batch.status != "completed": raise`) — there's no separate kill-signal lifecycle to get right, because polling-and-checking-status doesn't have one.

**Per-row failure stays per-row.** Both: a malformed or provider-rejected response lands as a flagged row (`invalid_output` for us, structurally the same idea as Airflow's `invalid_output` row type) rather than failing the whole batch. Same instinct, same name even, arrived at independently.

## Where Airflow is ahead

**`BatchAdapter` is real extensibility we don't have.** Dispatch by `model_id` prefix, through a built-in table, `register_adapter()`, or an entry point in the `airflow.providers.common.ai.batch_adapters` group — Bedrock and Vertex batch (different SDK shapes, S3/GCS-based) can ship as separate packages without touching `common.ai` at all. We have two separate, hardcoded, single-provider components instead of one pluggable abstraction. If a third or fourth batch-capable provider shows up, Airflow's shape scales to it more gracefully than ours does today.

**They thought harder about Jinja.** `@task.llm_batch` deliberately does not render returned prompts, because batch inputs are bulk text the DAG author didn't write, and a stray `{{` could resolve `var`/`conn` accessors against Airflow secrets. Our `prompt_template` uses plain `str.format(**row_dict)` — row *values* substituted into placeholders the pipeline author wrote are not themselves re-interpreted as template syntax, so the specific injection class they're guarding against doesn't apply the same way here. Different mechanism, not a direct parity point, but worth being precise about rather than claiming a win that isn't really a comparison.

## The gap this comparison found — and fixed

Airflow's PR is explicit about something ours wasn't: *"Intent is recorded before the paid submit call, so a crash between 'request sent' and 'response recorded' is recoverable on OpenAI through batch metadata; Anthropic offers no such lookup and falls through to an explicit `on_orphaned_intent` policy."*

Checking our own code against that sentence found the same unhandled race: if the process died between `batches.create()` succeeding (a paid call) and this asset's materialization recording that batch's id, the next run had no record of it — and would submit a second, duplicate, paid batch.

Fixed both, honestly, to the actual capability of each provider's API rather than papering over the difference:

- **OpenAI** genuinely supports the Airflow-style recovery: `batches.create()` already took a `metadata` field (we use it for `prompts_hash`), and `batches.list()` exists. Before any fresh submit, scan recent batches for one already tagged with the current hash and reattach instead of creating a new one.
- **Anthropic's batch API has no metadata field at all** — confirmed against the installed SDK, not assumed. There's no lookup to fall back on, which is exactly what Airflow's own PR says about Anthropic too. So: the same `on_orphaned_intent` idea, implemented with what Dagster offers instead of a metadata field — an `AssetObservation` records submission intent immediately before the paid call; a later run that finds an unresolved intent for the same hash raises with recovery instructions by default, or proceeds if explicitly told to accept the risk (`on_orphaned_intent: resubmit`).

Both verified against fake clients simulating an actual crash (the fake `create()` succeeds, then raises — "paid call completed, process died before the function returned") before any of this shipped.

## Try it

[`openai_llm_batch`](https://dagster-component-ui.vercel.app/c/openai_llm_batch) and [`anthropic_llm_batch`](https://dagster-component-ui.vercel.app/c/anthropic_llm_batch) — each component page has the full field reference and a working `example.yaml`. (No dedicated `setup_*_demo.sh` for either yet — if that'd be useful, it's a natural next addition to this examples directory.)
