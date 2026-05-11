# NLP utilities — 6 standalone NLP transforms

**Validated end-to-end** — RUN_SUCCESS in seconds. Synthetic article
corpus (LLM-generated) feeds 5 local NLP transforms; a separate Q&A
dataset feeds text similarity.

```
synthetic_data (gpt-4o-mini → 30 software-topic articles)
       │
       ├── chunked_articles            ← document_chunker (recursive split)
       ├── article_word_frequencies    ← word_cloud (top-N word frequencies)
       ├── pos_tagged_articles         ← part_of_speech_tagger (spaCy en_core_web_sm)
       └── topic_modeled_articles      ← topic_modeler (sklearn LDA, n_topics=5)

document_pairs (hand-crafted Q&A, 7 pairs)
       │
       └── qa_similarity_scores        ← text_similarity (cosine TF-IDF)
```

## Components covered (6)

| Component | Backend |
|---|---|
| [`synthetic_data`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/synthetic_data) | OpenAI (gpt-4o-mini) — generates rows matching a YAML-declared schema |
| [`document_chunker`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/document_chunker) | Pure stdlib string-splitting — `recursive`, `fixed`, `sentence`, `token_aware` strategies |
| [`word_cloud`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/word_cloud) | Pure pandas word counting — `top_n`, `frequency_table`, `tfidf` modes |
| [`part_of_speech_tagger`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/part_of_speech_tagger) | spaCy `en_core_web_sm` (downloaded by setup script) |
| [`topic_modeler`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/topic_modeler) | sklearn LatentDirichletAllocation with TF-IDF features |
| [`text_similarity`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/text_similarity) | TF-IDF cosine similarity (no model download needed for `cosine_tfidf` method) |

## Cost

~**$0.05** for 30 article generations against `gpt-4o-mini`.
Everything else is local.

## Required env var

```bash
OPENAI_API_KEY=sk-...
```

(only needed for [`synthetic_data`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/synthetic_data); the other 5 components run offline)

## Run it

```bash
./setup_nlp_utilities_demo.sh
cd nlp-utilities-demo
uv run dg launch --assets '*'
```

Or open the asset graph:

```bash
uv run dg dev   # http://localhost:3000
```

## Patterns to copy

- [`synthetic_data`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/synthetic_data) is the easiest way to bootstrap demos that need
  realistic-looking text. Pair with a downstream NLP component to see
  the full path end-to-end.
- [`document_chunker`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/document_chunker) produces a row-per-chunk DataFrame — pipe it into
  [`vector_store_writer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/vector_store_writer) (in `setup_vector_rag_demo.sh`) for a full RAG
  ingestion pipeline.
- [`topic_modeler`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/topic_modeler) returns the topic ID per row plus a topic-keywords
  column — combine with [`summarize`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/summarize) (in `setup_dataframe_basics_demo.sh`)
  to count documents-per-topic.
- [`text_similarity`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/text_similarity) with `method: cosine_tfidf` is dependency-free.
  Set `method: sentence_transformers` + `model_name: all-MiniLM-L6-v2`
  for embedding-based similarity (downloads ~80MB model).
