# Trustworthy RAG — a real trust layer on top of retrieve-and-generate

**Components:** `embeddings_generator`, `vector_store_writer`, `rag_pipeline`, `rag_grounding_check`, `llm_judge` — all existing, composed via YAML.

**Script:** [`setup_trustworthy_rag_demo.sh`](./setup_trustworthy_rag_demo.sh)
**Cost:** ~$0.01 (3 short `gpt-4o-mini` completions for `generate` + 3 judge calls; embeddings run locally via sentence-transformers)
**Validated:** 2026-10-31 — RUN_SUCCESS end-to-end; `answer_grounding` and `answer_quality` checks both evaluated and passed against 3 real generated answers.

## Why this exists

Classic RAG is retrieve-and-hope: the answer looks plausible, but is it actually grounded in what was retrieved, or is the model padding with fabricated details? This demo adds a real trust layer on top of a minimal retrieve→generate chain — both as real Dagster asset checks you'd see in the UI, not printed text.

```
knowledge_corpus ──► kb_embeddings ──► kb_index (chromadb)
rag_queries ─────────────────────────────┐
                                          ▼
                                     rag_answers
                                     ├──► answer_grounding  (rag_grounding_check, mode: overlap — FREE)
                                     └──► answer_quality    (llm_judge — real LLM call)
```

`rag_grounding_check`'s `mode: overlap` is n-gram-overlap based — no LLM, no API key, and it was explicitly designed to consume `rag_pipeline`'s native `sources` shape (a list of `{text: ...}` dicts) with zero transformation, confirmed by reading both components' source directly. `llm_judge` is a second, independent LLM-as-judge pass scoring accuracy/clarity/completeness (0–10) — this one genuinely needs a real model call.

## Prerequisites

- `uv` — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- `OPENAI_API_KEY` — used for `rag_pipeline`'s `generate` step and `llm_judge`. Embeddings (both indexing and query-time retrieval) use `sentence-transformers` locally, so this is the only paid dependency.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_trustworthy_rag_demo.sh -o setup_trustworthy_rag_demo.sh
chmod +x setup_trustworthy_rag_demo.sh
./setup_trustworthy_rag_demo.sh
```

## What you'll see

1. 5 small KB docs embedded locally and indexed into a local ChromaDB collection.
2. For each of 3 queries: top-3 docs retrieved, then `gpt-4o-mini` answers using only those docs as context.
3. `answer_grounding` — flags fabricated numbers/claims the retrieved docs don't actually support.
4. `answer_quality` — a real LLM-as-judge pass scoring every answer.

```bash
cd trustworthy_rag_demo
uv run dg dev
```
- Click `rag_answers` to see each query/answer/sources row.
- Click `answer_grounding` → Checks tab for the `grounding_quality` check. It passes trivially on a first run (nothing to compare against yet) — rerun to see real cross-materialization regression detection.
- Click `answer_quality` for `judge_score`/`judge_reason` per answer.

## Make it fail on purpose

Ask a question the KB can't answer (e.g. "What is Dagster's revenue?"). `gpt-4o-mini` will likely fabricate a plausible-sounding number, and `answer_grounding`'s overlap score will drop — a real, visible catch, not a staged one.

## Production variants

- Swap `mode: overlap` for `mode: llm_judge` on `rag_grounding_check` itself for a second, LLM-based groundedness opinion — costs more, catches paraphrased hallucinations overlap scoring misses.
- Swap `sentence_transformers` for `provider: openai` embeddings for higher-quality retrieval.
- Add a `rerank` step to `rag_pipeline`'s `steps:` (Voyage/Cohere) before `generate`.
