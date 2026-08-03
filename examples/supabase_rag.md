# Supabase pgvector RAG — Real RAG in ~200 lines of YAML

**Components:**
- `TextEmbeddingAssetComponent` (new — `assets/ai/text_embedding_asset`)
- `SupabaseResourceComponent` + `SupabaseVectorSearchAssetComponent` (`resources/supabase_resource`, `assets/ai/supabase_vector_search_asset`)
- `LangChainChainAssetComponent` (`assets/ai/langchain_chain_asset`)

**Script:** [`setup_supabase_rag_demo.sh`](./setup_supabase_rag_demo.sh)
**Cost:** ~$0.01 per run (embed + LLM answer)
**Validated:** 2026-07-07 — RUN_SUCCESS end-to-end; real 1536-d OpenAI embeddings + pgvector cosine search + LLM grounded to top-1 (similarity 0.701, off-topic docs correctly excluded).

## Why this exists

RAG is what everyone is building right now. The typical stack: vector DB + embeddings + retrieval + LLM. Done properly, this demo shows:

- **Zero cloud, zero account** — Supabase runs locally via the official `supabase` CLI. `docker compose` under the hood.
- **Real pgvector** — not a mock. Cosine similarity via a Postgres RPC using the `<=>` operator.
- **Real embeddings** — OpenAI `text-embedding-3-small` at 1536 dimensions.
- **Real grounding** — the LLM cites the retrieved doc titles inline. Off-topic docs get "context doesn't answer" per policy.
- **Pure YAML** — no bespoke Python. The whole pipeline is 4 `defs.yaml` files.

```
rag_query_embedding (OpenAI embedding of the question, 1 row × 1536-d)
        ↓
retrieved_context (pgvector RPC: cosine similarity, top-3 rows from `docs`)
        ↓
rag_answer (LangChain LLM per retrieved row, grounded via prompt template)
```

## The new component — `text_embedding_asset`

For RAG query embeddings and other "small literal string → embedding vector" needs. Complements `embeddings_generator` (which is row-wise over an upstream DataFrame). Pure YAML config, no Python glue.

```yaml
type: dagster_community_components.TextEmbeddingAssetComponent
attributes:
  asset_name: rag_query_embedding
  texts:
    - "How does Dagster orchestrate around long-running Temporal workflows?"
  model: text-embedding-3-small
  api_key_env_var: OPENAI_API_KEY
  dimensions: 1536
```

## Prerequisites

- `uv` — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- `docker` — Docker Desktop or engine
- `supabase` CLI — `brew install supabase/tap/supabase`
- `OPENAI_API_KEY` (get one at https://platform.openai.com/api-keys)

## Run

```bash
export OPENAI_API_KEY=sk-...
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_supabase_rag_demo.sh -o setup_supabase_rag_demo.sh
chmod +x setup_supabase_rag_demo.sh
./setup_supabase_rag_demo.sh
```

## What the script does

1. `supabase init` + `supabase start` — spins up 7 containers (Postgres, PostgREST, GoTrue, Storage, Realtime, Studio, Kong).
2. Creates a `docs (id, title, content, embedding vector(1536))` table + `match_docs(query, k)` RPC using pgvector's `<=>` operator.
3. Seeds 8 knowledge-base docs about the Dagster ecosystem + embeds them via OpenAI (1536-d).
4. Scaffolds a Dagster project + installs deps (dagster-community-components, supabase, openai, langchain).
5. Writes a `definitions.py` that wires a persistent `FilesystemIOManager` (necessary — the default ephemeral IO manager doesn't survive across `dagster asset materialize` invocations).
6. Writes four `defs.yaml`:
   - `supabase_resource` — env-var-backed Supabase client
   - `rag_query_embedding` — `TextEmbeddingAssetComponent` for the question
   - `retrieved_context` — `SupabaseVectorSearchAssetComponent` with `rpc_name: match_docs`
   - `rag_answer` — `LangChainChainAssetComponent` per-row LLM answer
7. Materializes each asset in order.

## Validated run output (2026-07-07)

```
--- Retrieved doc #0: [Temporal integration]  sim=0.701
  LLM answer: Dagster orchestrates around long-running Temporal workflows by
              observing them via `temporal_workflow_sensor`, triggering them
              using `temporal_workflow_trigger`, and managing state through
              `temporal_signal_asset` and `temporal_query_asset` [Temporal integration].

--- Retrieved doc #1: [Vercel deployment sensor]  sim=0.426
  LLM answer: The retrieved context does not answer the question about how
              Dagster orchestrates around long-running Temporal workflows.

--- Retrieved doc #2: [dbt + ML mid-DAG]  sim=0.425
  LLM answer: The retrieved context does not provide information on how
              Dagster orchestrates around long-running Temporal workflows.
```

This is exactly what good RAG should look like: the high-similarity doc drives the answer with inline citation; low-similarity docs get honest "context doesn't answer" responses.

## Adapting to your knowledge base

Replace the seed loop in the setup script with a real `docs_ingestion` step:

- **Google Drive** — `google_drive_ingestion` component (in the registry)
- **SharePoint** — `sharepoint_monitor` + downstream file reader
- **S3 / GCS / ADLS** — `s3_monitor` / `gcs_monitor` / `adls_monitor` + a text extraction chain
- **Confluence / Notion** — write a small ingestion component (both have REST APIs)

Chain that to `embeddings_generator` (row-wise) → upsert to Supabase via a small SQL asset. Then this same 4-asset RAG shape works over your live knowledge base.

## Why the persistent IO manager

Dagster's default in-memory / ephemeral IO manager doesn't survive between separate `dagster asset materialize` CLI invocations. When we materialize `rag_query_embedding` → then `retrieved_context` → then `rag_answer` in three commands, we need the intermediate DataFrames on disk. `FilesystemIOManager(base_dir=".dagster_storage")` handles that.

## See also

- [Cube semantic layer + LLM](./cube_query.md) — different pattern: **structured** metrics via Cube, no vector search. Use when your questions map cleanly to measures/dimensions.
- [LangGraph agent](./langgraph_agent.md) — multi-step reasoning. Could replace the single-step LangChain call for iterative refinement over retrieved context.
- [dbt + LLM mid-DAG](./dbt_llm_pipeline.md) — row-wise LLM enrichment. Same LangChain component, different upstream shape.
