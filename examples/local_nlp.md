# Local NLP mega-demo (13 components)

**Validated end-to-end** — 13 NLP / lightweight-AI components running on
30 synthetic support tickets. **8 are fully local (no API key)**, **4
use OpenAI** for LLM-driven tasks. Cost <$0.05.

```
support_tickets (synthetic_data_generator: 30 multilingual tickets)
       │
       ┌── LOCAL ─────────────────────────────────────────┐
       ├── document_chunks         (document_chunker: recursive 200/50)
       ├── text_chunks             (text_chunker: fixed_tokens 64/8)
       ├── ticket_pos_tags         (part_of_speech_tagger: spaCy)
       ├── ticket_topics           (topic_modeler: LDA, 5 topics)
       ├── ticket_word_cloud       (word_cloud: PNG output)
       ├── ticket_similarity       (text_similarity: cosine_tfidf vs query)
       ├── ticket_zero_shot        (zero_shot_classifier: HF facebook/bart-large-mnli)
       └── parsed_tickets          (llm_output_parser: list parser, no LLM call)
       └────────────────────────────────────────────────────┘
       ┌── LLM (OpenAI gpt-4o-mini) ───────────────────────┐
       ├── ticket_schema_fit       (schema_fit: LLM column-mapping plan)
       ├── ticket_match_classified (precision_match: LLM fuzzy → canonical)
       ├── ticket_classified       (ticket_classifier: LLM-mode classification)
       └── tickets_sql_query       (sql_generator: NL → SQL generation)
       └────────────────────────────────────────────────────┘
```

## Components used

### Local (no API key)

| Component | Asset | What it does |
|---|---|---|
| `document_chunker` | `document_chunks` | recursive char-based chunking, 200 chars / 50 overlap |
| `text_chunker` | `text_chunks` | fixed-tokens chunking, 64 tokens / 8 overlap |
| `part_of_speech_tagger` | `ticket_pos_tags` | spaCy `en_core_web_sm` POS tags per token |
| `topic_modeler` | `ticket_topics` | LDA / sklearn over the corpus, 5 topics |
| `word_cloud` | `ticket_word_cloud` | matplotlib + wordcloud PNG |
| `text_similarity` | `ticket_similarity` | cosine TF-IDF against a fixed query string |
| `zero_shot_classifier` | `ticket_zero_shot` | HuggingFace `bart-large-mnli` zero-shot |
| `llm_output_parser` | `parsed_tickets` | parses list / json / key_value text — no LLM call |

### LLM-driven (OpenAI gpt-4o-mini)

| Component | Asset | What it does |
|---|---|---|
| `schema_fit` | `ticket_schema_fit` | given a target schema, asks LLM to plan how to map source cols |
| `precision_match` | `ticket_match_classified` | maps varied input strings → canonical values |
| `ticket_classifier` | `ticket_classified` | classify into category/urgency/sentiment/department |
| `sql_generator` | `tickets_sql_query` | natural-language question → SQL |

## Run

```bash
export OPENAI_API_KEY='sk-...'   # needed for 4 of 13 components

curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_local_nlp_demo.sh | bash
cd local-nlp-demo
uv run dg launch --assets '*'
```

The setup script auto-installs `spacy en_core_web_sm` (~12MB) and
warms HuggingFace `bart-large-mnli` on first run (~1.6GB download —
takes a few minutes the first time).

## Cost

~$0.05 — the 8 local components are free; the 4 LLM components share
gpt-4o-mini calls (~30 rows × 4 calls = ~120 cheap completions).
