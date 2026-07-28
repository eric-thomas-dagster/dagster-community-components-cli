# End-to-End RAG in Dagster

Two parallel RAG paths over the same 5-doc corpus, in one Dagster project. Runs no-key by default; drop in an OpenAI-compatible key to enable the final LLM answer step.

- **Path A** — the state-tracking shape. Corpus → snapshot → eval, with an asset check that fails on retrieval-quality regressions. Use this when you need corpus provenance, index rollback via partition selection, and quality-gated downstream materialization.
- **Path B** — the classic decomposed pipeline. Chunker → embeddings → vector store → retrieval → rerank → LLM answer. Every step is its own asset with its own history, its own re-run, its own materialization.

Both paths consume the same `docs_corpus` — that's the "shared source" edge.

## Asset graph

```
              ┌──────────────────────┐
              │  docs_corpus         │  DocumentCorpus (no key)
              │  (5 markdown docs)   │  metadata: doc_count, total_bytes, corpus_hash
              └──────────┬───────────┘  asset_check: min_doc_count
                         │
        ┌────────────────┼──────────────────────────────────────────────┐
        ▼                                                                ▼
  Path A — state-tracking                                        Path B — decomposed
                                                                        │
  ┌───────────────────────────┐                     ┌────────────────────────────────┐
  │ docs_index_snapshot       │                     │ docs_chunks                    │
  │ (VectorIndexSnapshot)     │                     │ (DocumentChunker, fixed)       │
  │ • chunk + embed + index   │                     │ • splits corpus into chunks    │
  │ • registers dynamic       │                     └────────────┬───────────────────┘
  │   partition per snapshot  │                                  ▼
  │ • Chroma default embedder │                     ┌────────────────────────────────┐
  │   (no key)                │                     │ chunk_embeddings               │
  └────────────┬──────────────┘                     │ (EmbeddingsGenerator,          │
               │                                    │  sentence_transformers/MiniLM) │
               ▼                                    └────────────┬───────────────────┘
  ┌───────────────────────────┐                                  ▼
  │ docs_eval[snapshot_id]    │                     ┌────────────────────────────────┐
  │ (RagEval)                 │                     │ docs_vector_index              │
  │ • golden set + precision@k│                     │ (VectorStoreWriter → Chroma)   │
  │ • asset_check fires on    │                     └────────────┬───────────────────┘
  │   regression vs prior     │                                  │
  │   materialization         │                                  │
  └───────────────────────────┘                                  │
                                                                 │
                                    ┌────────────────────────────┤
                                    ▼                            │
                     ┌──────────────────────────┐                │
                     │ queries                  │                │
                     │ (DataframeFromCsv)       │                │
                     └────────────┬─────────────┘                │
                                  ▼                              │
                     ┌──────────────────────────┐                │
                     │ query_embeddings         │                │
                     │ (EmbeddingsGenerator,    │                │
                     │  same model as chunks)   │                │
                     └────────────┬─────────────┘                │
                                  ▼                              │
                     ┌──────────────────────────┐                │
                     │ retrieved                │◀───────────────┘
                     │ (VectorStoreQuery, top-k)│
                     └────────────┬─────────────┘
                                  ▼
                     ┌──────────────────────────┐
                     │ reranked                 │
                     │ (Reranker, cross-encoder │
                     │  local, no key)          │
                     └────────────┬─────────────┘
                                  ▼
                     ┌──────────────────────────┐
                     │ rag_answer               │
                     │ (LLMPromptExecutor)      │
                     │ requires OPENAI_API_KEY  │
                     │ (or swap provider)       │
                     └──────────────────────────┘
```

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_rag_complete_demo.sh \
  -o setup_rag_complete_demo.sh
bash setup_rag_complete_demo.sh
```

Requirements: `uv`, ~2 GB one-time install (`sentence-transformers` + `torch` + `chromadb`). ~3 min first run, ~30 s thereafter.

Optional: `export OPENAI_API_KEY=sk-...` before running to include the LLM answer step. Without it, everything materializes through `reranked`; you can wire the LLM later by exporting the key and running `dg launch --assets rag_answer`.

## Sample output — with `OPENAI_API_KEY` set

```
Q: How do I configure retry policy?
A: To configure a retry policy, set it on any asset by using:
   RetryPolicy(max_retries=3, delay=30, backoff=Backoff.EXPONENTIAL)
   This will allow the op to be re-executed up to 3 times with a 30-second
   delay between attempts, using an exponential backoff strategy...

Q: What is a dynamic partition?
A: A dynamic partition is a feature that lets you add partition keys at
   runtime using DynamicPartitionsDefinition. You register new keys through
   context.instance.add_dynamic_partitions(...)...

Q: How do I write an asset check?
A: Attach an @asset_check to the asset. Return AssetCheckResult(passed=bool,
   severity=AssetCheckSeverity.ERROR, metadata={...}). Failing checks with
   severity ERROR block downstream materializations...
```

Every answer is grounded — the LLM prompt template only exposes retrieved chunk content. If the retrieved context doesn't answer the question, the system-prompt instructs the model to say so explicitly.

## Components used

| Layer | Component | Path | No key? |
|---|---|---|---|
| Source | `DocumentCorpus` | A + B | ✓ |
| Chunk + embed + index (fused, versioned) | `VectorIndexSnapshot` | A | ✓ |
| Retrieval quality gate | `RagEval` | A | ✓ |
| Chunker | `DocumentChunker` | B | ✓ |
| Embedder | `EmbeddingsGenerator` (`sentence_transformers`) | B | ✓ |
| Vector store | `VectorStoreWriter` (ChromaDB) | B | ✓ |
| Queries source | `DataframeFromCsv` | B | ✓ |
| Query embedder | `EmbeddingsGenerator` | B | ✓ |
| Retrieval | `VectorStoreQuery` (ChromaDB) | B | ✓ |
| Reranker | `Reranker` (`cross_encoder` local) | B | ✓ |
| LLM answer | `LLMPromptExecutor` (OpenAI-compatible) | B | ✗ |

**Both paths run over the same 5-doc corpus.** Path A gives you the state-tracking + rollback story from `rag_state.md`; Path B gives you a fully decomposed pipeline that lets you swap any single step (a different chunker, a different embedder, a different vector store) without rewriting the graph.

## Swap the LLM

Edit `src/<project>/defs/rag_answer/defs.yaml`. `LLMPromptExecutor` supports OpenAI, Anthropic, Gemini, and any OpenAI-compatible endpoint:

```yaml
# Anthropic
provider: anthropic
model: claude-3-5-sonnet-latest
api_key: ${ANTHROPIC_API_KEY}

# Gemini
provider: gemini
model: gemini-1.5-flash
api_key: ${GOOGLE_API_KEY}

# Local via Ollama (OpenAI-compatible)
provider: openai
model: llama3.1
api_key: none
# Point the client at http://localhost:11434/v1/ via api_base_env_var if the component supports it,
# or set OPENAI_BASE_URL in your shell before dg dev.
```

## When to reach for which path

- **Prototype / demo.** Path A alone. Corpus → snapshot → eval, no LLM step, 3 assets total, no keys. Shows the state-tracking story and gets a golden-set eval running.
- **Production RAG.** Path A + Path B. Path A tracks state and gates quality; Path B does the actual per-request retrieval + generation. `docs_index_snapshot` gives you versioned index snapshots; `docs_vector_index` is what your live query traffic hits.
- **Bring-your-own-vector-store.** Path B only — swap `VectorStoreWriter` and `VectorStoreQuery` from `chromadb` to `pinecone` / `weaviate` / `qdrant` / `pgvector` by changing `provider` + `connection_string`. Nothing else changes.

## Two other RAG walkthroughs in this repo

- **[`rag_state.md`](rag_state.md)** — the state-tracking pattern only (Path A here) with a *regression injection* demo: strip key terms from a doc, watch the next snapshot's asset check fail on its own.
- **[`vector_rag.md`](vector_rag.md)** — the classic decomposed pipeline (Path B here) with `RAGPipelineComponent` for the end-to-end retrieve+generate step in one component.

If you're new to RAG on Dagster, start here (`rag_complete.md`). If you're focused on the "why is this better than a pipeline" story specifically, read `rag_state.md`.
