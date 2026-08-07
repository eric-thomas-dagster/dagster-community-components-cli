# One-Component RAG, Driven by Dynamic Queries
> ⚠️ **Dagster+ Serverless / Hybrid:** deploys with modifications — emits absolute local path ($PROJECT_ABS) into defs.yaml — Serverless container won't have that path.

`rag_pipeline` is the "one-component RAG" shape — embed, retrieve, and generate in a single asset. This walkthrough shows how to drive it with dynamic per-partition queries (not a hardcoded string or env var), so each query gets its own asset materialization, its own run history, and its own retry semantics.

Contrast with the decomposed shape in [rag_complete.md](rag_complete.md), where every step in the pipeline (chunker, embedder, retriever, reranker, LLM) is its own asset. Both patterns are useful — the one-component shape is what you reach for when you don't need to swap individual steps and just want "queries in, answers out."

## The dynamic-queries idea

Queries live in a DataFrame, one row per query, with a `query_id` column. `rag_answer` is partitioned by `query_id`. Each partition materializes exactly one row's embed → retrieve → generate, tracked in Dagster's asset history with its own run.

```
docs_corpus → docs_chunks → chunk_embeddings → docs_vector_index
                                                       │
                                                       │
queries (DataframeFromCsv)  ──────────────────────     │
   query_id | question                             │   │
   q1       | How do I configure retry policy?     │   │
   q2       | What is a dynamic partition?         │   │
   q3       | How do I write an asset check?       │   │
                                                   ▼   │
                                          rag_answer[query_id]  ◀── deps: docs_vector_index
                                          (rag_pipeline)
                                          partition_type: static
                                          partition_values: q1,q2,q3
                                          partition_static_column: query_id
```

Materialize a single question:

```bash
dg launch --assets rag_answer --partition q2
```

Add a new question — no pipeline change:

```bash
echo "q4,How do freshness policies work?" >> data/queries.csv
# then update rag_answer's partition_values to include q4
dg launch --assets rag_answer --partition q4
```

Backfill everything:

```bash
dg launch --assets rag_answer --partition-range q1..q3
```

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_rag_pipeline_dynamic_demo.sh \
  -o setup_rag_pipeline_dynamic_demo.sh
bash setup_rag_pipeline_dynamic_demo.sh
```

Requirements: `uv`, ~2 GB one-time install (`sentence-transformers` + `torch` + `chromadb`). ~3 min first run.

Optional: `export OPENAI_API_KEY=sk-...` before running to actually execute the LLM answers. Without it, the vector index is built and `queries` is loaded; the RAG step is skipped with an inline note. Swap `llm_provider` to `anthropic` / `gemini` / an OpenAI-compatible endpoint in the yaml if preferred.

## Key `defs.yaml` shape

The interesting part is `rag_answer`. Everything upstream is a standard unpartitioned index build.

```yaml
type: dagster_community_components.RAGPipelineComponent
attributes:
  asset_name: rag_answer
  upstream_asset_key: queries        # DataFrame with query_id + question rows
  query_column: question             # pulls query text from this column
  answer_column: answer              # writes generated answer here
  sources_column: sources            # writes retrieved chunks here

  vector_store_provider: chromadb
  collection_name: docs_kb
  vector_store_connection: ${PROJECT_ABS}/vector_index

  embedding_provider: sentence_transformers   # MUST match the indexer
  embedding_model: all-MiniLM-L6-v2

  llm_provider: openai
  llm_model: gpt-4o-mini
  llm_api_key: ${OPENAI_API_KEY}
  top_k: 3
  temperature: 0.2

  # ─── The dynamic-queries part ────────────────────────────────
  partition_type: static
  partition_values: "q1,q2,q3"        # add keys → add partitions
  partition_static_column: query_id   # filters upstream to this partition's row

  deps:
    - docs_vector_index               # ordering-only; loaded via connection_string
  group_name: query
```

## Why partition instead of running once with the whole DataFrame?

The `rag_pipeline` component happily runs unpartitioned — hand it a DataFrame with N rows and it processes all N in one materialization. That works fine for a batch. Where per-partition shines:

- **Per-query re-run.** One question flakes on rate limit → re-run just that partition, not the whole batch.
- **Per-query history.** Materialization history shows `q2` at `2026-07-28 14:03` succeeded, `q2` at `2026-07-28 15:10` succeeded — you can diff answers across model versions per question.
- **Per-query cost + latency metadata.** Each partition's run carries its own metadata: how long that specific question took, how many tokens it burned.
- **Per-query retry policy.** Configure `retry_policy_max_retries: 3` on `rag_answer` — retries apply per partition, so a flaky rate-limit on `q2` doesn't restart `q1` and `q3`.
- **Concurrency control.** Partition-scoped concurrency limits let you say "at most 5 questions running against gpt-4o at once" without adding a queue in your app.

Downside: overhead. Each partition spins up its own run. If you're processing 10k queries, batch by group (partition by `customer_id` or `date`, not per-row). If you're processing dozens, per-query is worth it.

## Add or drop queries without rewriting the graph

```bash
# 1. Add the row
echo "q4,How do freshness policies work?" >> data/queries.csv

# 2. Extend rag_answer's partition_values in defs.yaml:
#      partition_values: "q1,q2,q3,q4"

# 3. Re-materialize queries + run the new partition:
dg launch --assets queries
dg launch --assets rag_answer --partition q4
```

The old partitions' materializations stay in history. Their retrieved sources are still queryable via the UI. That's the "queries are addressable state" story.

## Swap the LLM

`rag_pipeline` supports OpenAI, Anthropic, and any OpenAI-compatible endpoint on the LLM side:

```yaml
# Anthropic
llm_provider: anthropic
llm_model: claude-3-5-sonnet-latest
llm_api_key: ${ANTHROPIC_API_KEY}

# OpenAI-compatible local (Ollama, llama.cpp): set OPENAI_BASE_URL to the local
# endpoint before dg dev, keep llm_provider: openai. api_key can be "none".
```

Embeddings on the query side can be `openai` or `sentence_transformers` — they **must** match the model used to build the index. Mismatched embedders is silently wrong: retrieval returns nearest-in-the-wrong-space and the answers grade well on English fluency but poorly on faithfulness.

## When to reach for this shape vs the decomposed one

- **rag_pipeline (this walkthrough).** You're not swapping steps. You want per-query addressability, retry, cost tracking. Fewer moving parts.
- **[rag_complete.md](rag_complete.md) — decomposed.** You want to A/B a different chunker, a different reranker, or a different vector store without touching the LLM step. Each step is its own asset with its own history.
- **[rag_state.md](rag_state.md) — state-tracking only.** You care about corpus provenance and rollback via snapshot partitions, not per-query addressability.

You can combine them. Add `rag_pipeline` as a downstream of `docs_index_snapshot` (Path A in rag_complete), take `deps: [docs_index_snapshot]`, and you get corpus versioning + per-query addressability in the same graph.

## Components used

| Layer | Component | No key? |
|---|---|---|
| Source | [document_corpus](../c/document_corpus) | ✓ |
| Chunker | [document_chunker](../c/document_chunker) | ✓ |
| Embedder (index side) | [embeddings_generator](../c/embeddings_generator) (`sentence_transformers`) | ✓ |
| Vector store | [vector_store_writer](../c/vector_store_writer) (ChromaDB) | ✓ |
| Queries source | [dataframe_from_csv](../c/dataframe_from_csv) | ✓ |
| End-to-end RAG (embed + retrieve + generate) | [rag_pipeline](../c/rag_pipeline) | ✗ (LLM key) |

## See also

Browse the [walkthrough index](README.md) for related demos across every component family.
