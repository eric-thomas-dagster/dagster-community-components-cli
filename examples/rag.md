# RAG in Dagster — pick your shape

Five walkthroughs, one thesis: **RAG in production isn't a pipeline. It's a set of stateful entities.**

Every artifact — the corpus, the vector index, the retrieval-quality score, the prompt — becomes a first-class asset with lineage, versions, and checks. Rollback stops being a filesystem restore and becomes a partition selector. Quality regressions stop being Slack threads and become asset-check-gated blocks on downstream materialization.

Which walkthrough you want depends on where you are in the RAG journey.

## Pick your shape

| Walkthrough | Shape | Requires | When |
|---|---|---|---|
| **[rag_state.md](rag_state.md)** | Corpus → snapshot → eval (3 assets). Includes an **injected regression** that trips the retrieval-quality asset check on its own. | No keys | You want the "why is this better than a pipeline" story in one runnable demo, ~3 min. |
| **[rag_complete.md](rag_complete.md)** | Full pipeline: chunker → embeddings → vector store → retrieval → rerank → LLM answer, **plus** a state-tracking overlay (corpus + snapshot + eval) on the same corpus. | No key up through `reranked`; OpenAI-compatible for final answer | You're building production RAG and want to see every component in the collection wired together over one corpus. |
| **[rag_supervisor.md](rag_supervisor.md)** | Planner LLM reads a task, picks specialists from a bounded YAML-declared set, each pick is its own asset; a synthesizer combines. | OpenAI-compatible | Your task is multi-facet — no single retrieval is enough — and you want every runtime routing decision to be a named, replayable asset. |
| **[vector_rag.md](vector_rag.md)** | Classic embed → retrieve → rerank → RAG mega-demo, plus `conversation_memory` for multi-turn. | OpenAI | You want a compact single-project demo showing the traditional RAG stack with the OpenAI embedder + gpt-4o-mini. |
| **[supabase_rag.md](supabase_rag.md)** | pgvector-backed RAG on a local Supabase instance. | OpenAI + local Supabase CLI | Your target vector store is Postgres/pgvector; you want a real DB, not an embedded one. |

## Asset-graph shapes at a glance

**Retrieve-then-generate (`rag_complete` / `vector_rag` / `supabase_rag`):**

```
corpus → chunk → embed → index → retrieve → rerank → LLM answer
```

**State-tracking (`rag_state`, and overlay in `rag_complete`):**

```
corpus → snapshot (per-materialization partition) → eval[snapshot_id]
                                                    (asset_check gates regressions)
```

**Planner + specialists (`rag_supervisor`):**

```
                    ┌── specialist_a_result ──┐
task → plan  ───────┼── specialist_b_result ──┼── synthesis
                    └── specialist_c_result ──┘
```

## Recommended reading order

1. **`rag_state.md`** — the shortest path to seeing the state-tracking story land. No API keys. ~3 min.
2. **`rag_complete.md`** — every component in the collection wired end-to-end. Same corpus as `rag_state`. Compare the two paths side-by-side.
3. **`rag_supervisor.md`** — different orchestration pattern. Read this once you're comfortable with the linear pipeline and want to see what named-runtime-decisions look like.
4. **`vector_rag.md` / `supabase_rag.md`** — reach for these when your production vector store / conversation-memory story matters more than the state-tracking framing.

## The RAG component palette

Every walkthrough is 100% components — no custom Python in the `defs/` tree. Here's what the collection includes for RAG-adjacent work:

| Layer | Components |
|---|---|
| **Sources** | `DocumentCorpus`, `DataframeFromCsv`, `DataframeFromTable`, plus every DB/API/messaging component in the source library |
| **Chunking** | `DocumentChunker` (fixed, recursive, sentence, token-aware, semantic) |
| **Embeddings** | `EmbeddingsGenerator` (OpenAI, Cohere, sentence-transformers, HuggingFace), `TextEmbeddingAsset`, provider-specific: `AnthropicLLM`, `GeminiLLM`, `OpenAILLM`, etc. |
| **Vector stores** | `VectorStoreWriter` + `VectorStoreQuery` (Pinecone, Weaviate, ChromaDB, FAISS, Qdrant), `VectorIndexSnapshot` (versioned ChromaDB), `SupabaseVectorSearchAsset` |
| **Reranking** | `Reranker` (Cohere API or local cross-encoder) |
| **Retrieval / Generation** | `RAGPipelineComponent` (end-to-end retrieve+generate), `LLMPromptExecutorComponent`, `LangChainChainAsset` |
| **Agents** | `SupervisorAgent`, `IterativeSupervisorAgent`, `LangGraphAgent`, `CatalogAgent`, `PlannedCatalogAgent`, plus vendor-specific: `OpenAIAgent`, `AnthropicAgent`, `GeminiAgent`, `SnowflakeCortexAgent`, `VercelAiGatewayAgent` |
| **Evaluators** | `RagEval` (retrieval quality + regression gate), `LLMEvaluator`, `LLMJudge` |
| **Memory** | `ConversationMemory` |
| **Document extraction** | `DocumentTextExtractor`, `DocumentLayoutAnalyzer`, `DocumentAIExtractor`, `DocumentSummarizer`, `EntityExtractor` |

That's ~40 components you can compose. The five walkthroughs above are the well-lit paths; anything you build on top is a rearrangement of the same primitives.

## The one framing that matters

Whichever shape you pick, hold on to this: **the artifacts are the graph**. A materialization records a specific version of a specific artifact — the corpus at time T, the index built from corpus-hash X at time T, the eval score for snapshot Y against golden set Z. Six weeks later when someone asks "why did last Tuesday's answer look weird?", the graph tells you. That's the difference between a pipeline that runs and a system you can operate.
