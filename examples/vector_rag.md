# Vector / RAG mega-demo (5 components)

**Validated end-to-end** — embeddings → ChromaDB index → vector search →
local rerank → end-to-end RAG → conversation memory, all in one
pipeline using OpenAI embeddings + a local Chroma DB.

```
knowledge_corpus  ──► kb_embeddings ──► kb_index (chromadb)
(10 doc chunks)        (OpenAI text-embedding-3-small)         │
                                                              │
queries (5 Q's) ──► query_embeddings ──► search_results ──► reranked_results
                                         (top_k=3)            (cross-encoder/MiniLM-L-6 local)

rag_corpus (3 Q's) ──────────────────────────────► rag_response
                                                   (RAG: retrieve from kb_index, generate
                                                    answer with gpt-4o-mini)

chat_log (3 turns) ────► chat_history
                         (writes /tmp/chat_memory.json)
```

## Components used

| Component | Asset | Role |
|---|---|---|
| [`embeddings_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/embeddings_generator) (×2) | `kb_embeddings`, `query_embeddings` | OpenAI `text-embedding-3-small` over the corpus and the queries |
| [`vector_store_writer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/vector_store_writer) | `kb_index` | writes 10 vectors to ChromaDB persistent dir `/tmp/chroma_kb` |
| [`vector_store_query`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/vector_store_query) | `search_results` | top-3 retrieval per query against `kb_index` |
| [`reranker`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/reranker) | `reranked_results` | local `cross-encoder/ms-marco-MiniLM-L-6-v2` rerank — **no Cohere key** |
| [`rag_pipeline`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/rag_pipeline) | `rag_response` | end-to-end RAG: retrieve from same Chroma DB + answer with gpt-4o-mini |
| [`conversation_memory`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/conversation_memory) | `chat_history` | persists user/assistant turns into a JSON memory file |

## Bugs found + fixed during validation

1. **[`vector_store_writer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/vector_store_writer)** referenced an undefined variable `result`
   in the post-write metadata block — should have been `df`. Fixed.

2. **[`vector_store_query`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/vector_store_query)** didn't preserve the original query text in
   its output rows — only `query_row_index`, `id`, `document`,
   `metadata`, `distance`. This made downstream rerankers (which need
   both query and document side-by-side) impossible to wire up. Added
   an optional `query_text_column` field that copies the upstream
   query column into output rows.

3. **[`rag_pipeline`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/rag_pipeline)** field for the chromadb path is
   `vector_store_connection`, but [`vector_store_writer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/vector_store_writer) and
   [`vector_store_query`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/vector_store_query) use `connection_string`. Naming inconsistency
   left intact for backwards compat — documented in this demo's YAML.

4. **[`conversation_memory`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/conversation_memory), [`rag_pipeline`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/rag_pipeline)** had the recurring `ctx` →
   `context` asset-fn parameter bug. Already fixed in earlier sweep.

## Run

```bash
export OPENAI_API_KEY='sk-...'

curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_vector_rag_demo.sh | bash
cd vector-rag-demo
uv run dg launch --assets '*'
# Or in dev UI:
uv run dg dev   # → http://localhost:3000 → Assets graph
```

## Cost

~$0.05 — embeddings are cheap (~$0.02/1M tokens for `text-embedding-3-small`),
RAG generation uses ~3 short `gpt-4o-mini` completions.

## Trade-offs / next steps

- **ChromaDB** is local-first and zero-setup; for production swap for
  Pinecone, Weaviate, Qdrant, or pgvector by changing `provider` and
  the connection field.
- **Reranker uses cross-encoder** (local sentence-transformers) so this
  demo needs no Cohere key. Switch `method: cohere` + `api_key:
  ${COHERE_API_KEY}` for the managed reranking service.
- **[`rag_pipeline`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/rag_pipeline)** is end-to-end (retrieve+answer in one component).
  For more control (rerank between retrieve and answer, custom prompt
  templates), compose `vector_store_query → reranker → llm_prompt_executor`
  manually — that's exactly what the parallel branch in this demo
  exercises.

## See also

- [`llm_execution.md`](./llm_execution.md) — 12 LLM-orchestration components
- [`document_extractors.md`](./document_extractors.md) — 13 typed document extractors
- [`ai_with_llm.md`](./ai_with_llm.md) — task-specific LLM components
- [`ai_no_llm.md`](./ai_no_llm.md) — local AI components (no API key)
