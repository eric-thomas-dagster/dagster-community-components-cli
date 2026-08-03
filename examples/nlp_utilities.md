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

## Components used

| Component | Backend |
|---|---|
| `synthetic_data` | OpenAI (gpt-4o-mini) — generates rows matching a YAML-declared schema |
| `document_chunker` | Pure stdlib string-splitting — `recursive`, `fixed`, `sentence`, `token_aware` strategies |
| `word_cloud` | Pure pandas word counting — `top_n`, `frequency_table`, `tfidf` modes |
| `part_of_speech_tagger` | spaCy `en_core_web_sm` (downloaded by setup script) |
| `topic_modeler` | sklearn LatentDirichletAllocation with TF-IDF features |
| `text_similarity` | TF-IDF cosine similarity (no model download needed for `cosine_tfidf` method) |

## Cost

~**$0.05** for 30 article generations against `gpt-4o-mini`.
Everything else is local.

## Auth — OpenAI key required

`OPENAI_API_KEY` is **required** for this walkthrough because the
synthetic source asset uses gpt-4o-mini to generate the article corpus
that the 5 downstream NLP utilities operate on. The utilities themselves
are truly local (spaCy / scikit-learn / pandas / TF-IDF) — once you swap
the source for a real article DataFrame (CSV / DB / API), no API key is
needed.

```bash
OPENAI_API_KEY=sk-...
```

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_nlp_utilities_demo.sh | bash
cd nlp-utilities-demo
uv run dg launch --assets '*'
```

Or open the asset graph:

```bash
uv run dg dev   # http://localhost:3000
```

## Patterns to copy

- `synthetic_data` is the easiest way to bootstrap demos that need
  realistic-looking text. Pair with a downstream NLP component to see
  the full path end-to-end.
- `document_chunker` produces a row-per-chunk DataFrame — pipe it into
  `vector_store_writer` (in `setup_vector_rag_demo.sh`) for a full RAG
  ingestion pipeline.
- `topic_modeler` returns the topic ID per row plus a topic-keywords
  column — combine with `summarize` (in `setup_dataframe_basics_demo.sh`)
  to count documents-per-topic.
- `text_similarity` with `method: cosine_tfidf` is dependency-free.
  Set `method: sentence_transformers` + `model_name: all-MiniLM-L6-v2`
  for embedding-based similarity (downloads ~80MB model).

## See also

<!-- TODO: link related walkthroughs -->
