# Local Qwen Ticket Triage — fully offline, in-process open-weight LLM

**Components:** `synthetic_data_generator`, `text_moderator` (`method: transformer`), `filter`, `dataframe_to_csv` — all existing — plus a small plain `@asset` for drafting (see "Why a plain asset" below).

**Script:** [`setup_local_qwen_triage_demo.sh`](./setup_local_qwen_triage_demo.sh)
**Cost:** $0 — no API key, no cloud account. CPU inference on two small local models (`unitary/toxic-bert` ~400MB, `Qwen/Qwen2.5-0.5B-Instruct` ~1GB).
**Validated:** 2026-10-31 — RUN_SUCCESS end-to-end, fresh, byte-for-byte as shipped. 6 synthetic tickets generated, 2 flagged and filtered out by the local toxicity classifier, 4 real Qwen-drafted replies written to `approved_drafts.csv`.

## Why this exists

This repo already has an [Ollama demo](ollama.md) and litellm reaches any OpenAI-compatible self-hosted server (llama.cpp's `llama-server`, vLLM, etc.) via `model: openai/<name>` + a custom `api_base` — no new component needed for either. But both require a **separate server process** running alongside Dagster. That's fine locally, but **Dagster+ Serverless has no network path to a server running on your own laptop** — you'd need Hybrid with an agent on the same network, or a tunnel.

`transformers.pipeline()` loads the model **in-process** instead — no server, no port, no network call after the one-time Hub download. The exact same code runs identically on your laptop and inside a Dagster+ Serverless container. This demo is the in-process answer to "can we use a local, open-weight Qwen model" — genuinely portable, not just genuinely local.

## Why a plain `@asset` for drafting

No registry component does per-row **local** (`transformers.pipeline`-based) text generation over a DataFrame column today. [`huggingface_pipeline`](huggingface.md) is the closest existing component and *is* genuinely local/in-process, but by design takes a plain `inputs: [...]` list with no DataFrame in or out (its own docstring: *"For batch inference over a DataFrame column, use the task-specific components instead"* — there isn't one yet for text-generation specifically). `draft_replies` here is a small plain `@asset`, the same "glue where no component fits yet" convention [`self_healing_pipeline.md`](self_healing_pipeline.md) uses — and a real candidate for a future registry component.

## What it demonstrates

```
support_tickets_corpus (synthetic_data_generator)
       │
       ▼
moderated_tickets (text_moderator, method: transformer,
                    unitary/toxic-bert — local, no API key)
       │
       ▼
safe_tickets (filter: flagged == False)
       │
       ▼
draft_replies (plain @asset — transformers.pipeline("text-generation",
               Qwen/Qwen2.5-0.5B-Instruct), one draft per safe ticket)
       │
       ▼
approved_drafts.csv (dataframe_to_csv)
```

A real synthetic ticket corpus is moderated for toxicity entirely locally, filtered down to the legitimate tickets, and a real open-weight Qwen model drafts a reply for each — a realistic "AI drafts, nothing ships without a safety pass first" triage pipeline, fully offline.

## Prerequisites

- `uv` — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- ~2GB disk for the two model downloads (cached by HuggingFace after the first run); a few minutes on first run, seconds after.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_local_qwen_triage_demo.sh -o setup_local_qwen_triage_demo.sh
chmod +x setup_local_qwen_triage_demo.sh
./setup_local_qwen_triage_demo.sh
```

## What you'll see

A real run of this demo flagged 2 of 6 synthetic tickets (low toxicity scores, correctly below threshold) and drafted real, on-topic replies for the remaining 4 — e.g. a ticket asking "does the Pro plan include API access?" got back *"The Pro plan typically includes API access for some features and functionalities within the platform, but it's important to check specific documentation or contact customer support directly..."* — genuinely coherent output from a 0.5B-parameter model.

```bash
cd local_qwen_triage_demo
cat approved_drafts.csv
uv run dg dev
  → click draft_replies → see each ticket's real Qwen-generated draft in the preview metadata
  → click moderated_tickets → see the real toxicity/hate-speech/PII/profanity/sexual/violence scores per ticket
```

## Production variants

- Swap `Qwen/Qwen2.5-0.5B-Instruct` for a larger Qwen model if you have the compute (7B+ wants a GPU for reasonable latency).
- Swap `unitary/toxic-bert` for `s-nlp/roberta-toxicity-classifier`, or `method: openai_moderation` / `method: llm` if you'd rather use a hosted check.
- Swap `synthetic_data_generator` for your real ticket source (`upstream_asset_key` on `text_moderator` already points at any DataFrame-producing asset).
- Point `draft_replies` at a larger hosted model instead by swapping the `transformers.pipeline()` call for `litellm_inference_asset`/`litellm_agent` (any provider, including a real llama.cpp/Ollama server via `model: openai/<name>` + `api_base`) — the in-process approach here is the zero-infrastructure default, not the only option.
