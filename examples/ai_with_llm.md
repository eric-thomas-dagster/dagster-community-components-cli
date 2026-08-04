# AI components — LLM-powered (OpenAI / Azure OpenAI)
> ✅ **Dagster+ Serverless:** deploys as-is via `dagster-cloud serverless deploy-docker`.

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
| 1 | `synthetic_data_generator` | `support_tickets` | source — 30 multilingual tickets |
| 2 | `text_classifier` | `ticket_priorities` | classify into `[low, medium, high, urgent]` |
| 3 | `entity_extractor` | `ticket_entities` | extract people/orgs/products/IDs/emails |
| 4 | `sentiment_analyzer` | `ticket_sentiment` | sentiment + confidence + reasoning |
| 5 | `document_summarizer` | `ticket_summaries` | one-sentence summary |
| 6 | `data_enricher` | `ticket_urgency` | derive `urgent_action_required` (yes/no) + `primary_intent` |

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

## See also

<!-- TODO: link related walkthroughs -->
