# AI components — no LLM key required

**Validated end-to-end** — 30 synthetic multilingual support tickets fan out
through 5 local AI components. No OpenAI/Anthropic key needed.

```
synthetic_data_generator (schema_type: support_tickets)
       │  → support_tickets DataFrame (multilingual text + embedded PII)
       │
       ├── keyword_extractor    (TF-IDF top keywords)
       ├── language_detector    (ISO 639-1 codes via langdetect)
       ├── pii_detector         (counts of emails / phones / names)
       ├── pii_redactor         (PII replaced with placeholders)
       └── embeddings_generator (384-dim sentence-transformers vectors)
```

## Components used

| # | Component | Output |
|---|---|---|
| 1 | [`synthetic_data_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/synthetic_data_generator) (`schema_type: support_tickets`) | 30 tickets in en/es/fr/de with embedded PII + ground-truth labels |
| 2 | [`keyword_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/keyword_extractor) | TF-IDF top-k keywords per ticket |
| 3 | [`language_detector`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/language_detector) | ISO 639-1 language code per ticket |
| 4 | [`pii_detector`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/pii_detector) | counts of emails / phones / names found |
| 5 | [`pii_redactor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/pii_redactor) | ticket text with PII masked via Presidio |
| 6 | [`embeddings_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/embeddings_generator) (`provider: sentence_transformers`) | local 384-dim MiniLM-L6 vectors |

## Validated end-to-end

All 6 assets materialized — runtime ~60s, dominated by the one-time
sentence-transformers model download (~80MB). After warmup, full
materialization is sub-second per asset.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_ai_no_llm_demo.sh | bash
cd ai-no-llm-demo
uv run dg launch --assets '*'
# Or in dev UI:
uv run dg dev   # → http://localhost:3000 → Assets graph
```

## Cost

$0 — fully local. All models are open-weight (sentence-transformers,
spaCy, scikit-learn, langdetect, Presidio).

## See also

- [`ai_with_llm.md`](./ai_with_llm.md) — companion demo using the same
  upstream synthetic data, but routing through OpenAI/Azure OpenAI for
  classification, extraction, summarization, sentiment, and enrichment.
