# AI components — LLM-powered (OpenAI / Azure OpenAI)

**Validated end-to-end** — 30 synthetic support tickets fan out through 5
LLM-powered components, ~150 OpenAI API calls total against `gpt-4o-mini`.

```
synthetic_data_generator (schema_type: support_tickets)
       │  → support_tickets DataFrame
       │
       ├── text_classifier     → priority bucket per ticket (low/medium/high/urgent)
       ├── entity_extractor    → people, orgs, products, order IDs, emails
       ├── sentiment_analyzer  → sentiment + confidence
       ├── document_summarizer → 1-sentence summary per ticket
       └── data_enricher       → urgent_action_required + primary_intent
```

All five LLM components share the same `_make_openai_client` helper, so
setting `OPENAI_AZURE_ENDPOINT` (instead of `OPENAI_API_KEY`) routes the
same code through Azure OpenAI — no code changes.

## Components used

| # | Component | Asset | LLM purpose |
|---|---|---|---|
| 1 | [`synthetic_data_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/synthetic_data_generator) | `support_tickets` | source — 30 multilingual tickets |
| 2 | [`text_classifier`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/text_classifier) | `ticket_priorities` | classify into `[low, medium, high, urgent]` |
| 3 | [`entity_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/entity_extractor) | `ticket_entities` | extract people/orgs/products/IDs/emails |
| 4 | [`sentiment_analyzer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/sentiment_analyzer) | `ticket_sentiment` | sentiment + confidence + reasoning |
| 5 | [`document_summarizer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/document_summarizer) | `ticket_summaries` | one-sentence summary |
| 6 | [`data_enricher`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/data_enricher) | `ticket_urgency` | derive `urgent_action_required` (yes/no) + `primary_intent` |

## Validated end-to-end

| Asset | Wall-clock | What |
|---|---|---|
| `support_tickets` | 57ms | 30 synthetic tickets generated |
| `ticket_urgency` | 7s | data_enricher (2 fields × 30 rows) |
| `ticket_sentiment` | 23s | per-row LLM sentiment + confidence |
| `ticket_summaries` | 23s | per-row one-sentence summary |
| `ticket_priorities` | 24s | per-row classification into 4-way bucket |
| `ticket_entities` | 27s | per-row entity extraction (5 types) |

Total: 5 LLM components × 30 tickets = ~150 OpenAI calls in ~30 seconds
(parallel execution via Dagster's multiprocess executor). Cost on
`gpt-4o-mini`: well under $0.50.

## Bugs found + fixed during validation

While building this demo, four real bugs surfaced in the registry and
were fixed:

1. **`text_classifier.categories` typed as `str`** (expecting a JSON
   string) — Dagster's YAML resolver auto-parses `[...]` as a list
   regardless of quoting, so the field could never accept what its
   docstring suggested. Changed to `List[str]` + updated example.yaml +
   schema.json. User config is now natural YAML (`categories: [low,
   medium, high, urgent]`).

2. **6 AI components named the asset-fn first parameter `ctx` instead of
   `context`** — Dagster only treats the special name `context` as the
   `AssetExecutionContext`; any other name is interpreted as an *input
   asset*. Result: every materialization failed with `Input asset
   "['ctx']" is not produced by any...`. Fixed across
   [`text_classifier`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/text_classifier), [`document_summarizer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/document_summarizer), [`rag_pipeline`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/rag_pipeline),
   [`llm_chain_executor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/llm_chain_executor), [`llm_output_parser`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/llm_output_parser), [`conversation_memory`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/conversation_memory).

3. **[`sentiment_analyzer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/sentiment_analyzer) / [`entity_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/entity_extractor) / [`text_moderator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/text_moderator)** used
   `prompt_template.format(text=text)` to interpolate the user text into
   a template that contained literal JSON braces (`{"sentiment":
   "..."}`), causing `KeyError` since `str.format()` interprets `{` as a
   field marker. Switched to `prompt_template.replace("{text}", text)`.

4. **[`text_classifier`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/text_classifier) had only-`json.loads`** as its way of parsing
   `categories`, which broke once the field became a list. Now passes
   the list through directly.

## Run

```bash
export OPENAI_API_KEY='sk-...'

curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_ai_with_llm_demo.sh | bash
cd ai-with-llm-demo
uv run dg launch --assets '*'
# Or in dev UI:
uv run dg dev   # → http://localhost:3000 → Assets graph
```

### Azure OpenAI

The exact same setup works against Azure OpenAI. Set these env vars
*instead* of `OPENAI_API_KEY`:

```bash
export OPENAI_AZURE_ENDPOINT='https://<your-resource>.openai.azure.com'
export OPENAI_AZURE_API_VERSION='2024-10-21'
export OPENAI_AZURE_USE_ENTRA=1   # if using managed identity / SP auth
```

The `_make_openai_client` helper detects `OPENAI_AZURE_ENDPOINT` and
routes through `AzureOpenAI` instead of `OpenAI` — same components, same
YAML, no code changes.

## Cost

~$0.10–$0.50 per full pipeline run on `gpt-4o-mini` (30 tickets × 5
components ≈ 150 calls).
