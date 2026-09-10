# pgvector_reader demo (Docker Postgres + pgvector)
> ❌ **Dagster+ Serverless / Hybrid:** local-only — requires Docker for pgvector-enabled Postgres.
> ✅ **No external API keys required** — uses the `query_embedding` literal path.

Proves `pgvector_reader` end-to-end against a seeded Docker Postgres
with the pgvector extension. The reader supports two query modes:
`query_text` (embedded via OpenAI at runtime — needs API key) or
`query_embedding` (pre-computed vector literal). We use the literal
path so the demo is hermetic.

```
Docker Postgres 16 + pgvector
  │  documents(id, title, embedding vector(4))  — 8 hand-tuned rows on a small grid
  │  IVFFLAT cosine index for realistic ANN behavior
  ▼
pgvector_reader
  │  query_embedding: [1.0, 0.0, 0.0, 0.0]   ← literal, no embedder called
  │  distance_metric: cosine (<=>)
  │  n_results: 5
  ▼
similar_docs asset (ranked hits as DataFrame)
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `pgvector_reader` | source | ANN search against a pgvector table; produces ranked hits as a Dagster asset |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_pgvector_reader_demo.sh | bash
cd pgvector-reader-demo
export DATABASE_URL='postgresql+psycopg2://postgres:demo@localhost:5433/postgres'

uv run dg check defs
uv run dg launch --assets similar_docs
```

Expected in the run log:

```
similar_docs - Querying documents for top 5 results using cosine distance
similar_docs - Retrieved 5 results
similar_docs - ASSET_MATERIALIZATION - Materialized value similar_docs.
```

## Verify the ranking

The 8 seed rows are hand-tuned on a 4-dim conceptual grid so cosine
similarity to `[1.0, 0.0, 0.0, 0.0]` produces a deterministic top-K:

```bash
docker exec -it dg-pgvector-demo psql -U postgres -c \
  "SELECT id, title,
          ROUND((1 - (embedding <=> '[1.0, 0.0, 0.0, 0.0]'))::numeric, 3) AS similarity
     FROM documents ORDER BY embedding <=> '[1.0, 0.0, 0.0, 0.0]' LIMIT 5;"
```

Expected:

```
 id |     title     | similarity
----+---------------+------------
  1 | ml basics     |      0.995
  2 | ml advanced   |      0.970
  3 | ml frameworks |      0.930
  4 | databases 101 |      0.100
  5 | sql tuning    |      0.000
```

`ml basics` closest (nearly aligned with query axis), `databases 101`
farthest (orthogonal). Same result comes back through the Dagster
asset — the reader is doing exactly what direct SQL would.

## Two query modes

| Mode | When to use | Cost |
|---|---|---|
| `query_embedding: [...literal...]` | You've pre-computed the query vector (via any embedder — Voyage / Cohere / LiteLLM / local model / etc.) OR you want deterministic tests | Free at query time |
| `query_text: "some text"` | You want the component to embed the query for you at runtime | Requires an OpenAI API key (`api_key_env_var`) — the reader currently only supports OpenAI for the embedding step; use the literal path for other providers |

For production RAG, pair `pgvector_reader` in `query_text` mode with a
matching embedder upstream (same model that produced the stored
vectors, so the query and corpus live in the same embedding space).

## Retargeting to production

Swap `DATABASE_URL` for any Postgres-with-pgvector instance — RDS,
Cloud SQL, Neon, Supabase, self-hosted. No component-side changes.

## Cleanup

```bash
docker rm -f dg-pgvector-demo
```

## See also

- **`pgvector_asset`** — sibling component that WRITES embeddings into a
  pgvector table. Currently locked to OpenAI for embedding generation —
  see [TODO in the source repo](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/TODO.md)
  for the LiteLLM-refactor follow-up that will unlock every embedding
  provider (Voyage / Cohere / Ollama / local sentence-transformers /
  etc.). Once that lands, the two components pair naturally for
  end-to-end RAG.
- **`litellm_embedding_batch`** — the canonical multi-provider
  embedding-generation component.
- Browse the [walkthrough index](README.md).
