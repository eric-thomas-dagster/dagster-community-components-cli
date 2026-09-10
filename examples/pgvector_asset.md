# pgvector_asset demo (Ollama + Docker Postgres, both embedding paths)
> ❌ **Dagster+ Serverless / Hybrid:** local-only — requires Docker for both Ollama and pgvector-enabled Postgres.
> ✅ **No external API keys required** — Ollama runs the embedder locally.

Proves the refactored `pgvector_asset` end-to-end, exercising **both**
embedding paths in the same run so you can see the composition options
side-by-side.

```
synthetic_data_generator (100 fake products)
     │
     ├── Path A (inline) ────────────────────────────────────────────────┐
     │                                                                    │
     │   pgvector_asset (embedding_model: ollama/nomic-embed-text,        │
     │                    api_base_env_var: OLLAMA_HOST)                  │
     │   ↓                                                                │
     │   Postgres: product_embeddings_inline (100 rows × vector(768))     │
     │                                                                    │
     └── Path B (precomputed) ────────────────────────────────────────────┤
                                                                          │
         litellm_embedding_batch (model: ollama/nomic-embed-text) ────►   │
         adds `embedding` column to the DataFrame                         │
              ↓                                                           │
         pgvector_asset (precomputed_embedding_column: embedding)         │
         skips the embedder call entirely                                 │
              ↓                                                           │
         Postgres: product_embeddings_precomputed (100 × vector(768))     │
                                                                          │
Both target tables contain BYTE-FOR-BYTE IDENTICAL vectors (same model,
same rows, same order) — proving the two paths are equivalent.
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `synthetic_data_generator` | ai | 100 fake products with `product_id` + `name` columns |
| 2 | `pgvector_asset` (inline) | analytics | Embeds inline via LiteLLM+Ollama, upserts into `product_embeddings_inline` |
| 3 | `litellm_embedding_batch` | ai | Adds `embedding` column to upstream via LiteLLM+Ollama |
| 4 | `pgvector_asset` (precomputed) | analytics | Skips embedder, upserts `product_embedded.embedding` column into `product_embeddings_precomputed` |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_pgvector_asset_demo.sh | bash
cd pgvector-asset-demo
export DATABASE_URL='postgresql+psycopg2://postgres:demo@localhost:5434/postgres'
export OLLAMA_HOST='http://localhost:11435'

uv run dg check defs
uv run dg launch --assets '*'
```

Expected in the run log:

```
raw_products - Generated DataFrame with shape (100, ...)

[pgvector] Received 100 rows from upstream asset.
[pgvector] Embedding 100 texts via LiteLLM (model='ollama/nomic-embed-text', api_base=http://localhost:11435)
[pgvector] Upsert complete: 100 rows in 'product_embeddings_inline'.

Generating embeddings for 100 rows with model=ollama/nomic-embed-text ...

[pgvector] Received 100 rows from upstream asset.
[pgvector] Using precomputed embeddings from column 'embedding' (skipping embedder call — 100 rows).
[pgvector] Upsert complete: 100 rows in 'product_embeddings_precomputed'.
```

## Verify both tables landed identically

```bash
docker exec -it dg-pgvector-asset-demo psql -U postgres -c \
  "SELECT 'inline' AS path, COUNT(*) AS rows, MAX(vector_dims(embedding)) AS dim
     FROM product_embeddings_inline
    UNION ALL
   SELECT 'precomputed', COUNT(*), MAX(vector_dims(embedding))
     FROM product_embeddings_precomputed;"
```

Expected:

```
    path     | rows | dim
-------------+------+-----
 inline      |  100 | 768
 precomputed |  100 | 768
```

And to prove the two paths produce byte-for-byte identical vectors
(same model, same inputs, same order):

```bash
docker exec -it dg-pgvector-asset-demo psql -U postgres -c \
  "SELECT COUNT(*) FILTER (WHERE i.embedding = p.embedding) AS matching_vectors,
          COUNT(*) AS total_rows
     FROM product_embeddings_inline i
     JOIN product_embeddings_precomputed p USING (id);"
```

Expected: `matching_vectors=100, total_rows=100` — all vectors match.

## Which path should I use?

| | **Inline** | **Precomputed** |
|---|---|---|
| YAML | 1 component | 2 components (embedder + asset) |
| Retries | Batch-level (whole embed+upsert re-runs) | Per-step (embed and upsert re-run independently) |
| Provider swap | Change `embedding_model` on pgvector_asset | Change `model` on litellm_embedding_batch |
| Cost visibility | One materialization event per table | Separate metadata for embedding vs upsert |
| Fallback | Use pgvector_asset's failure-retry only | `litellm_embedding_batch` supports `fallback_models: [...]` |
| Reuse | Rebuild vectors on every upsert | Cache upstream, re-upsert without re-embedding |

Rule of thumb: **inline for one-shot chains and simple demos**;
**precomputed when embedding is the expensive step or you need
per-step observability, fallbacks, or vector reuse.**

## Provider swap (no infra change)

Change `embedding_model` (and `dimensions` to match), leave everything
else the same:

| Provider | `embedding_model` | `dimensions` | Auth |
|---|---|---|---|
| OpenAI | `text-embedding-3-small` | 1536 | `api_key_env_var: OPENAI_API_KEY` |
| OpenAI (large) | `text-embedding-3-large` | 3072 | `api_key_env_var: OPENAI_API_KEY` |
| Voyage AI | `voyage/voyage-3` | 1024 | `api_key_env_var: VOYAGE_API_KEY` |
| Cohere | `cohere/embed-english-v3.0` | 1024 | `api_key_env_var: COHERE_API_KEY` |
| Ollama (LOCAL) | `ollama/nomic-embed-text` | 768 | `api_base_env_var: OLLAMA_HOST` |
| Azure OpenAI | `azure/<deployment>` | model-dep | `api_key_env_var` + `api_base_env_var` |

## Cleanup

```bash
docker rm -f dg-pgvector-asset-demo dg-ollama-demo
```

## Historical note

Before v1.2.0, `pgvector_asset` was locked to OpenAI (hardcoded
`openai.embeddings.create` call, `openai_api_key_env_var` required).
Now:

- The inline path uses **LiteLLM** — every provider it supports works
  by changing `embedding_model`.
- The `precomputed_embedding_column` field is a full escape hatch —
  compose with any embedding-producing component upstream.
- `openai_api_key_env_var` still resolves for backwards compatibility
  (pydantic alias) — no YAML change needed for existing users.

## See also

- **[pgvector_reader](pgvector_reader.md)** — sibling that reads vectors + queries by similarity.
- **[litellm_embedding_batch](https://dagster-component-ui.vercel.app/c/litellm_embedding_batch)** — the multi-provider embedder used in Path B.
- Browse the [walkthrough index](README.md).
