# Azure AI Search Round-Trip
> ✅ **Dagster+ Serverless:** deploys as-is via `dagster-cloud serverless deploy-docker`.

**Validated end-to-end** against live infrastructure.

30 synthetic products → indexed in Azure AI Search → query (text
search + filter + sort) → CSV report. Foundation pattern for RAG,
semantic search, enterprise knowledge bases.

```
synthetic_data_generator → azure_search_indexer → azure_search_query → dataframe_to_csv
                                  │                       │
                                  └─→ Azure AI Search ────┘
                                       (products-demo index)
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `synthetic_data_generator` | ai | 30 synthetic products |
| 2 | `azure_search_indexer` | sink | Push docs to index (mergeOrUpload) |
| 3 | `azure_search_query` | source | Filter `total gt 500`, order by total desc, top 100 |
| 4 | `dataframe_to_csv` | sink | High-value report |

## Validated end-to-end

Tested against a Free-tier Azure AI Search service. 5 documents indexed
in <1s; search query for `ergonomic` with `price > 25` filter returned
the 2 expected hits.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_azure_search_demo.sh | bash
cd azure-search-demo
uv run dg launch --assets '*'
```

## Vector + semantic search

For vector search (embeddings stored in the index), pre-compute embeddings
upstream (e.g. with `embeddings_generator`) and add a vector field to the
index schema. The indexer component handles vectors via the same
mergeOrUpload action — just include the vector column.

For semantic ranker (Azure's built-in re-ranking model), set
`semantic_configuration_name` on the query component (requires Standard
tier and a configured semantic config in the index).

## Cost

| Tier | Cost | Limits |
|---|---|---|
| Free | $0/mo | 50 MB / 3 indexes / no SLA |
| Basic | ~$0.10/hr (~$74/mo) | 2 GB / 5 indexes / 99.9% SLA |
| Standard S1 | ~$0.34/hr (~$250/mo) | 25 GB / 50 indexes / semantic ranker |

## See also

<!-- TODO: link related walkthroughs -->
