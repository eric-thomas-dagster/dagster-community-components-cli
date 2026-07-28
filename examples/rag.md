# RAG in Dagster — pick your shape

Five walkthroughs, one thesis: **RAG in production isn't a pipeline. It's a set of stateful entities.**

Every artifact — the corpus, the vector index, the retrieval-quality score, the prompt — becomes a first-class asset with lineage, versions, and checks. Rollback stops being a filesystem restore and becomes a partition selector. Quality regressions stop being Slack threads and become asset-check-gated blocks on downstream materialization.

Which walkthrough you want depends on where you are in the RAG journey.

## Pick your shape

| Walkthrough | Shape | Requires | When |
|---|---|---|---|
| **[rag_state.md](rag_state.md)** | Corpus → snapshot → eval (3 assets). Includes an **injected regression** that trips the retrieval-quality asset check on its own. | No keys | You need retrieval-quality regressions to block downstream automatically, or you want rollback to a past index snapshot via partition selection. |
| **[rag_complete.md](rag_complete.md)** | Full pipeline: chunker → embeddings → vector store → retrieval → rerank → LLM answer, **plus** a state-tracking overlay (corpus + snapshot + eval) on the same corpus. | No key up through `reranked`; OpenAI-compatible for final answer | You're building a full retrieve-then-generate pipeline over your own docs and want to see every step as its own asset before adapting it. |
| **[rag_supervisor.md](rag_supervisor.md)** | Planner LLM reads a task, picks specialists from a bounded YAML-declared set, each pick is its own asset; a synthesizer combines. | OpenAI-compatible | Your task needs multiple specialist responses instead of one retrieval, and you need every routing decision inspectable weeks later. |
| **[vector_rag.md](vector_rag.md)** | Classic embed → retrieve → rerank → RAG mega-demo, plus `conversation_memory` for multi-turn. | OpenAI | You want the classic OpenAI + ChromaDB + rerank stack with multi-turn conversation memory. |
| **[supabase_rag.md](supabase_rag.md)** | pgvector-backed RAG on a local Supabase instance. | OpenAI + local Supabase CLI | Your target vector store is Postgres/pgvector — you want a real DB backing the index, not an embedded one. |

## Asset-graph shapes at a glance

**Retrieve-then-generate ([RAG complete](rag_complete.md) / [Vector / RAG](vector_rag.md) / [Supabase pgvector RAG](supabase_rag.md)):**

```
corpus → chunk → embed → index → retrieve → rerank → LLM answer
```

**State-tracking ([RAG state-tracking](rag_state.md), and overlay in [RAG complete](rag_complete.md)):**

```
corpus → snapshot (per-materialization partition) → eval[snapshot_id]
                                                    (asset_check gates regressions)
```

**Planner + specialists ([RAG supervisor](rag_supervisor.md)):**

```
                    ┌── specialist_a_result ──┐
task → plan  ───────┼── specialist_b_result ──┼── synthesis
                    └── specialist_c_result ──┘
```

## Recommended reading order

1. **[RAG state-tracking](rag_state.md)** — corpus + snapshot + eval, no API keys, ~3 min. See what an asset-check-gated regression looks like.
2. **[RAG complete](rag_complete.md)** — same corpus as RAG state-tracking, plus a full retrieve-then-generate pipeline running alongside. See every step of the pipeline as its own asset.
3. **[RAG supervisor](rag_supervisor.md)** — different orchestration pattern. Read this when a single retrieval isn't enough and you need a planner + specialists.
4. **[Vector / RAG](vector_rag.md) / [Supabase pgvector RAG](supabase_rag.md)** — reach for these when your target vector store (ChromaDB with conversation memory / pgvector on Supabase) is the deciding factor.

## The RAG component palette

| Layer | Components |
|---|---|
| **Sources** | [`document_corpus`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/document_corpus), [`dataframe_from_csv`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sources/dataframe_from_csv), [`dataframe_from_table`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sources/dataframe_from_table), plus every DB/API/messaging component in the [source library](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sources) |
| **Chunking** | [`document_chunker`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/document_chunker) (fixed, recursive, sentence, token-aware, semantic) |
| **Embeddings** | [`embeddings_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/embeddings_generator) (OpenAI, Cohere, sentence-transformers, HuggingFace), [`text_embedding_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/text_embedding_asset); provider-native: [`openai_llm`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/openai_llm), [`anthropic_llm`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/anthropic_llm), [`gemini_llm`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/gemini_llm) |
| **Vector stores** | [`vector_store_writer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/vector_store_writer) + [`vector_store_query`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/vector_store_query) (Pinecone, Weaviate, ChromaDB, FAISS, Qdrant), [`vector_index_snapshot`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/vector_index_snapshot) (versioned ChromaDB), [`supabase_vector_search_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/supabase_vector_search_asset) |
| **Reranking** | [`reranker`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/reranker) (Cohere API or local cross-encoder) |
| **Retrieval / Generation** | [`rag_pipeline`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/rag_pipeline) (end-to-end retrieve+generate), [`llm_prompt_executor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/llm_prompt_executor), [`langchain_chain_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/langchain_chain_asset) |
| **Agents** | [`supervisor_agent`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/supervisor_agent), [`iterative_supervisor_agent`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/iterative_supervisor_agent), [`langgraph_agent`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/langgraph_agent), [`catalog_agent`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/catalog_agent), [`planned_catalog_agent`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/planned_catalog_agent); provider-native: [`openai_agent`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/openai_agent), [`anthropic_agent`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/anthropic_agent), [`gemini_agent`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/gemini_agent), [`snowflake_cortex_agent`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/snowflake_cortex_agent), [`vercel_ai_gateway_agent`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/vercel_ai_gateway_agent) |
| **Evaluators** | [`rag_eval`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/rag_eval) (retrieval quality + regression gate), [`llm_evaluator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/llm_evaluator), [`llm_judge`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/llm_judge) |
| **Memory** | [`conversation_memory`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/conversation_memory) |
| **Document extraction** | [`document_text_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/document_text_extractor), [`document_layout_analyzer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/document_layout_analyzer), [`document_ai_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/document_ai_extractor), [`document_summarizer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/document_summarizer), [`entity_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/entity_extractor) |

~40 components you can compose. The five walkthroughs above are well-lit paths through them; anything you build on top is a rearrangement of the same primitives.

## Artifacts are the graph

Whichever shape you pick, hold on to this: **the artifacts are the graph**. A materialization records a specific version of a specific artifact — the corpus at time T, the index built from corpus-hash X at time T, the eval score for snapshot Y against golden set Z. Six weeks later when someone asks "why did last Tuesday's answer look weird?", the graph tells you. That's the difference between a pipeline that runs and a system you can operate.
