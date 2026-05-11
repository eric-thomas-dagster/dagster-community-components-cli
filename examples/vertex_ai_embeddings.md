# Vertex AI Text Embeddings — text → 768-dim vectors

**Validated end-to-end against real Vertex AI.** 5 product descriptions →
5 × 768-dim embeddings via `text-embedding-004`, materialized via the
actual Dagster component, written to CSV.

```
sample_texts                  (5 product descriptions)
       │
       └── sample_text_embeddings   ← vertex_ai_text_embeddings_asset
                                     (text-embedding-004, RETRIEVAL_DOCUMENT)
                  │
                  └── embeddings_flat       ← pandas (head 5 dims for CSV-readability)
                            │
                            └── embeddings_csv  ← /tmp/vertex_embeddings.csv
```

## Components covered (1)

| Component | What it does |
|---|---|
| [`vertex_ai_text_embeddings_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/vertex_ai_text_embeddings_asset) | Native Vertex AI text-embedding wrapper (text-embedding-004, gemini-embedding-001, multilingual-002, etc.). Drop-in shape parallel to [`embeddings_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/embeddings_generator) and the openai/anthropic embedding peers. Useful for RAG, semantic search, vector store loaders. |

## Validation status — live

Real run output (head 5 dims of each embedding):

| sku | dim | head |
|---|---|---|
| MUG-001 | 768 | -0.0152, -0.0521, 0.0113, -0.0165, 0.0180 |
| BAG-002 | 768 | 0.0043, -0.0043, 0.0294, 0.0127, 0.0196 |
| RUN-003 | 768 | -0.0288, -0.0264, 0.0035, -0.0321, 0.0063 |
| WTC-004 | 768 | -0.0022, 0.0141, -0.0045, -0.0209, 0.0342 |
| BRN-005 | 768 | -0.0066, -0.0056, 0.0090, -0.0292, 0.0597 |

All 5 distinct vectors as expected.

## Cost

**~$0.0001.** `text-embedding-004` is $0.025 / 1M input chars. The first
100K chars/month are free; the demo's 5 short rows cost effectively
nothing.

## Required env var

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
```

Required SA role: `roles/aiplatform.user` + Vertex AI API enabled.

## Run it

```bash
./setup_vertex_ai_embeddings_demo.sh
cd vertex-ai-embeddings-demo
uv run dg launch --assets '*'
cat /tmp/vertex_embeddings.csv
```

## Why use this vs other embedding components

| Component | Provider | Best for |
|---|---|---|
| [`vertex_ai_text_embeddings_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/vertex_ai_text_embeddings_asset) | Google Vertex AI | GCP-native shops, multilingual via gemini-embedding-001 |
| `openai_embeddings` / [`embeddings_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/embeddings_generator) (LiteLLM) | OpenAI | text-embedding-3-small/large, broad ecosystem |
| `anthropic_*` | (Anthropic doesn't have native embedding models — use Voyage AI or OpenAI for that) | — |

## Task-type tuning

Vertex tunes embeddings for the intended use:

| `task_type` | When |
|---|---|
| `RETRIEVAL_DOCUMENT` | Indexing docs for later retrieval (default in this demo) |
| `RETRIEVAL_QUERY` | Search queries against an index |
| `SEMANTIC_SIMILARITY` | Pairwise similarity scoring |
| `CLASSIFICATION` | Feeding into a classifier |
| `CLUSTERING` | k-means input |
| `QUESTION_ANSWERING` | Q+A retrieval |
| `FACT_VERIFICATION` | Fact-check workflows |
| `CODE_RETRIEVAL_QUERY` | Code search |

For RAG: index docs with `RETRIEVAL_DOCUMENT`, query with `RETRIEVAL_QUERY` — Vertex produces vectors that compose well together.

## Drop-in extensions

Truncate embeddings to save vector-store space (newer models support this):

```yaml
output_dimensionality: 256       # or 512 / 1024 instead of full 768
```

Switch model:

```yaml
model_name: gemini-embedding-001                # latest Gemini-family
model_name: text-multilingual-embedding-002     # multilingual
```
