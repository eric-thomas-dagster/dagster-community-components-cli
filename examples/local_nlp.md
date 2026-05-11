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
| [`document_chunker`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/document_chunker) | `document_chunks` | recursive char-based chunking, 200 chars / 50 overlap |
| [`text_chunker`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/text_chunker) | `text_chunks` | fixed-tokens chunking, 64 tokens / 8 overlap |
| [`part_of_speech_tagger`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/part_of_speech_tagger) | `ticket_pos_tags` | spaCy `en_core_web_sm` POS tags per token |
| [`topic_modeler`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/topic_modeler) | `ticket_topics` | LDA / sklearn over the corpus, 5 topics |
| [`word_cloud`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/word_cloud) | `ticket_word_cloud` | matplotlib + wordcloud PNG |
| [`text_similarity`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/text_similarity) | `ticket_similarity` | cosine TF-IDF against a fixed query string |
| [`zero_shot_classifier`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/zero_shot_classifier) | `ticket_zero_shot` | HuggingFace `bart-large-mnli` zero-shot |
| [`llm_output_parser`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/llm_output_parser) | `parsed_tickets` | parses list / json / key_value text — no LLM call |

### LLM-driven (OpenAI gpt-4o-mini)

| Component | Asset | What it does |
|---|---|---|
| [`schema_fit`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/schema_fit) | `ticket_schema_fit` | given a target schema, asks LLM to plan how to map source cols |
| [`precision_match`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/precision_match) | `ticket_match_classified` | maps varied input strings → canonical values |
| [`ticket_classifier`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/ticket_classifier) | `ticket_classified` | classify into category/urgency/sentiment/department |
| [`sql_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/sql_generator) | `tickets_sql_query` | natural-language question → SQL |

## Bugs found + fixed during validation

1. **[`document_chunker`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/document_chunker)** rejected `output_column` (extra field) — its
   actual fields are `source_column`, `strategy`, `chunk_size`,
   `chunk_overlap`. Demo YAML fixed.

2. **[`text_chunker`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/text_chunker)** required field `chunking_strategy` (not just
   `strategy`); didn't accept `output_column`. Fixed.

3. **[`text_chunker`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/text_chunker)** referenced undefined `chunks` variable in its
   metadata block (should have been `result_df`). Same `result/df`
   pattern bug as [`vector_store_writer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/vector_store_writer). Fixed.

4. **[`text_similarity`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/text_similarity)** field for the fixed comparison string is
   `query`, not `reference_text`. Documented.

5. **[`topic_modeler`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/topic_modeler)** uses `n_topics`, not `num_topics`. Documented.

6. **[`schema_fit`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/schema_fit)** missing `target_schema` was the real required field
   (not optional). Demo YAML now provides it.

7. **[`schema_fit`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/schema_fit)** didn't pass `response_format={"type":
   "json_object"}` to the LLM, so the model returned natural-language
   explanations and `json.loads` failed. Added `response_format`.

8. **[`precision_match`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/precision_match)** required fields are `column` (which column to
   standardize) and `reference_values` (canonical list). Demo YAML
   updated.

9. **[`sql_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/sql_generator)** required field is `question_column`. Documented.

10. **[`ticket_classifier`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/ticket_classifier)** had the recurring `prompt_template.format()`
    bug — its template builds dynamic JSON examples with literal `{`
    and `}` braces, which `.format()` interprets as field markers.
    Switched to manual `.replace()` per context variable. Same fix
    pattern as [`sentiment_analyzer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/sentiment_analyzer) / [`entity_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/entity_extractor) / [`text_moderator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/text_moderator).

11. **[`llm_output_parser`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/llm_output_parser)** had the `ctx → context` asset-fn parameter
    bug. Already fixed in earlier sweep.

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

## See also

- [`llm_execution.md`](./llm_execution.md) — 12 LLM-orchestration components
- [`document_extractors.md`](./document_extractors.md) — 13 typed document extractors
- [`vector_rag.md`](./vector_rag.md) — 5 vector-store / RAG components
- [`ai_with_llm.md`](./ai_with_llm.md) — task-specific LLM components
- [`ai_no_llm.md`](./ai_no_llm.md) — local AI components (no API key)
