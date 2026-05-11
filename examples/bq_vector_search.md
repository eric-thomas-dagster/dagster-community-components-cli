# BigQuery Vector Search — semantic retrieval over BQ embeddings

**Validated end-to-end against real APIs** (servicepulse-490502, demo_docs_embedded table).
2 query vectors → top-3 matches each, semantically correct ordering.

```
doc_search    ← bigquery_vector_search_asset
                (queries servicepulse-490502.dagster_demo.demo_docs_embedded)
```

## Component covered (1)

| Component | What it does |
|---|---|
| [`bigquery_vector_search_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/source/bigquery_vector_search_asset) | Wraps BigQuery's native `VECTOR_SEARCH` function for k-NN similarity over an `ARRAY<FLOAT64>` column. Two input modes: inline `query_vectors` (static) or `upstream_asset_key` + `query_vector_column` (per-row from upstream embedding asset). |

## Live run output

The demo table has 5 docs in 2 semantic clusters (billing vs. account) plus 1 outlier. Two query vectors retrieve their respective clusters:

| query_id | doc_id | content | distance |
|---|---|---|---|
| q0 (billing intent) | d1 | Refund my credit card | 0.001382 |
| q0 | d2 | Cancel my subscription | 0.001686 |
| q0 | d4 | Update my email address | 0.994196 (filler) |
| q1 (account intent) | d3 | Reset my password | 0.001382 |
| q1 | d4 | Update my email address | 0.001686 |
| q1 | d2 | Cancel my subscription | 0.994196 (filler) |

Top-2 results per query land in the correct cluster. The third result is a distant "filler" — exactly what you'd expect with `top_k: 3` on a 5-row table.

## Bugs surfaced fixing this demo

1. **`VECTOR_SEARCH` SQL syntax was wrong** — initial code combined `distance_type` + `use_brute_force` into a single `options` clause like `'{distance_type=>'COSINE', use_brute_force=>true}'`. Real syntax: `distance_type` and `top_k` are **top-level named args**, and `options` is a JSON STRING for tuning params only (`use_brute_force`, `fraction_lists_to_search`). Fixed in the component.

## SQL emitted (after fix)

```sql
SELECT
  'q0' AS query_id,
  base.doc_id, base.content,
  distance
FROM VECTOR_SEARCH(
  TABLE `project.dataset.demo_docs_embedded`,
  'embedding',
  (SELECT [0.95, 0.05, 0.0, 0.0] AS query_vec),
  query_column_to_search => 'query_vec',
  top_k => 3,
  distance_type => 'COSINE',
  options => '{"use_brute_force": true}'
)
```

## Performance — vector indexes

Without `use_brute_force: true`, BigQuery uses a vector index when one exists. Create one for tables > ~5K rows:

```sql
CREATE VECTOR INDEX my_index
ON `project.dataset.documents`(embedding)
OPTIONS(index_type='IVF', distance_type='COSINE')
```

Tune ANN recall via `fraction_lists_to_search` (0–1, higher = more accurate, more expensive). The demo uses `use_brute_force: true` because 5 rows is too small for an index to be meaningful.

## Two input modes

**Static** (this demo) — inline vectors in YAML, run once.
**From upstream** — one BQ search per row of an upstream DataFrame. Typical RAG flow:
```yaml
upstream_asset_key: query_embeddings
query_vector_column: vector
query_id_column: query_id
```
Upstream produced by e.g. [`vertex_ai_text_embeddings_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/vertex_ai_text_embeddings_asset) (text-embedding-004, 768-dim).

## Required env vars

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json
export GCP_PROJECT_ID=your-project
export BQ_DATASET=your_dataset
```

## Required IAM

- `roles/bigquery.dataEditor` (on the dataset, to create the demo table)
- `roles/bigquery.dataViewer` (to read the table)
- `roles/bigquery.jobUser` (project, to run queries)

## Run it

```bash
./setup_bq_vector_search_demo.sh
cd bq-vector-search-demo
uv run dg launch --assets '*'
```
