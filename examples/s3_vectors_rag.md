# AWS S3 Vectors — a real embed → write → query RAG round-trip, zero cost
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

**Components:** `synthetic_data_generator`, `embeddings_generator`, `vector_store_writer` (new `provider: s3vectors`), `vector_store_query` (new `provider: s3vectors`) — all existing, composed via YAML.

**Script:** [`setup_s3_vectors_rag_demo.sh`](./setup_s3_vectors_rag_demo.sh)
**Cost:** $0 (`demo_mode: true` on both s3vectors steps; embeddings run locally via sentence-transformers)
**Validated:** 2026-10-31 — RUN_SUCCESS end-to-end; 8 vectors written, 24 query results returned (top_k=3 × 8 queries), each ticket's own embedding ranked itself as the closest match.

## Why this exists

AWS S3 Vectors (GA since 2025-12-02) is native vector storage in S3 — no separate vector database to run or pay for if you're already on AWS. This registry's `VectorStoreWriterComponent`/`VectorStoreQueryComponent` already support `pinecone`/`weaviate`/`chromadb`/`faiss`/`qdrant`; `s3vectors` is a new provider option on the same two components (not a dedicated component — same dispatch pattern).

```
support_tickets_corpus (synth)
       │
       └── ticket_embeddings (sentence-transformers, local, no API key)
                │
                ├── s3_vector_index        — vector_store_writer, provider: s3vectors
                │
                └── ticket_search_results  — vector_store_query, provider: s3vectors
                                              (deps: [s3_vector_index] — see below)
```

## A real gotcha: query/write ordering

The query asset declares `deps: [s3_vector_index]` even though it doesn't read that asset's *data* — without it, Dagster can run the query **before** the write finishes in a multi-asset job, since they're otherwise independent branches off the same `ticket_embeddings` asset. This bit the first draft of this demo (`Found 0 total results for 8 queries`); the `deps:` fixes it. Worth keeping in mind for any job that chains these two components together.

## Prerequisites

- `uv` — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Nothing else — `sentence-transformers` downloads a small (~90MB) model on first run, no account needed.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_s3_vectors_rag_demo.sh -o setup_s3_vectors_rag_demo.sh
chmod +x setup_s3_vectors_rag_demo.sh
./setup_s3_vectors_rag_demo.sh
```

## Validated run (2026-10-31)

```
s3_vector_index  — Writing 8 embeddings to s3vectors
s3_vector_index  — Successfully wrote 8 vectors
ticket_search_results — Querying s3vectors for top 3 results for 8 rows
ticket_search_results — Found 24 total results for 8 queries
RUN_SUCCESS - Finished execution of run for "__ASSET_JOB".
```

Each query (one per ticket, using that ticket's own embedding) returned itself as the top match with `distance ≈ 0`, confirming the write → query round-trip and the cosine-similarity ranking both work correctly.

## Production variants

- Set `demo_mode: false` on **both** `vector_store_writer` and `vector_store_query`, plus a real `vector_bucket_name` your AWS credentials can write to. The bucket + index are created automatically on first write (`dataType: float32`, `distanceMetric: cosine`, hardcoded — matching how the existing Qdrant branch also hardcodes cosine rather than exposing a config field).
- Swap `provider: sentence_transformers` for `provider: openai` on `embeddings_generator` for higher-quality embeddings (needs `OPENAI_API_KEY`).
- Set `filter_metadata` on `vector_store_query` (e.g. `'{"priority": "high"}'`) — it maps directly onto S3 Vectors' own `filter` parameter and genuinely restricts results for this provider (note: `filter_metadata` is a pre-existing field on this component that isn't actually wired up for the other 4 providers — it's a real, functioning filter for `s3vectors` specifically).
- Hard limits worth knowing: `PutVectors` caps at 500 vectors/call (the writer batches automatically), dimension 1–4096, up to 2B vectors/index.

## See also

- [`vector_store_writer` README](https://raw.githubusercontent.com/eric-thomas-dagster/dagster-component-templates/main/assets/ai/vector_store_writer/README.md)
- [`vector_store_query` README](https://raw.githubusercontent.com/eric-thomas-dagster/dagster-component-templates/main/assets/ai/vector_store_query/README.md)
- [Supabase pgvector RAG](supabase_rag.md) — same RAG shape, a different managed-vector-store backend.
- Browse the [walkthrough index](README.md) for related demos across every component family.
