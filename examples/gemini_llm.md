# Gemini LLM — text generation on synthetic tickets

**Validated end-to-end against the live Gemini API.** RUN_SUCCESS in
1m7s for 20 synthetic support-ticket rows via `gemini-2.5-flash`.
Free-tier 5 RPM rate limit was hit during the run; the component
handled it gracefully with exponential backoff and the asset still
materialized cleanly.

```
support_tickets   ← synthetic_data_generator (20 rows)
        │
        └── ticket_summaries  ← gemini_llm (gemini-2.5-flash)
```

## Components covered (2)

| Component | What it does |
|---|---|
| `gemini_llm` | Native (no-LiteLLM) Google Gemini text LLM. Drop-in peer of `openai_llm` / `anthropic_llm` — same field shape, swap providers by changing `type:` and the api_key env var. |
| `synthetic_data_generator` | Generates synthetic DataFrames; `schema_type: support_tickets` produces 20 plausible support-ticket rows. |

## Cost

**Free.** `gemini-2.5-flash` has a generous Google AI Studio free
quota (5 RPM, ~1500 req/day for text). 20 ticket summaries fit
comfortably; the demo took ~67s total because the rate limiter
held a few rows.

## Required env var

```bash
GEMINI_API_KEY=...    # or GOOGLE_API_KEY (component falls back)
```

Get a key at <https://aistudio.google.com/app/apikey>.

## Run it

```bash
./setup_gemini_llm_demo.sh
cd gemini-llm-demo
uv run dg launch --assets '*'
```

Inspect the asset graph (the `ticket_summaries` asset's metadata
shows the model + row count + a markdown preview of the responses):

```bash
uv run dg dev   # http://localhost:3000
```

## Swap to a different Gemini model

Edit `src/<pkg>/defs/gemini_llm/defs.yaml` and change `text_model:`:

| Model | When to pick |
|---|---|
| `gemini-2.5-flash` | Default. Fast, cheap, great quality for most jobs. |
| `gemini-2.5-pro` | Most capable. Long-context analysis, complex reasoning. |
| `gemini-2.0-flash-lite` | Cheapest. Bulk classification / summarization. |
| `gemini-flash-latest` | Auto-track the latest flash minor version. |
| `gemini-pro-latest` | Auto-track the latest pro minor version. |
| `gemini-3-pro-preview` | Preview of the next generation. Requires Gemini preview tier on your key. |

Run `client.models.list()` against your key for the live set.

## Why a native (non-LiteLLM) component?

The registry already has `litellm_inference_asset` which routes to
OpenAI, Anthropic, and Gemini through one config. We ship `gemini_llm`
alongside it because:

- **Single-vendor shops** standardize on Google and don't want the
  LiteLLM router as an extra dep.
- **Drop-in field shape** — same `upstream_asset_key`, `input_column`,
  `output_column`, `system_prompt`, etc. as `openai_llm` /
  `anthropic_llm`, so you can A/B providers by changing the `type:`
  line.
- **Native error messages** — on 404 (bad model id) the component
  surfaces "Set `text_model:` to a current id" with concrete
  suggestions; on 429 it tells you exactly what to do.

## Why all three (openai_llm / anthropic_llm / gemini_llm)?

Drop-in equivalence + production multi-vendor patterns:

| Component | Provider | Common use |
|---|---|---|
| `openai_llm` | OpenAI | gpt-4o, gpt-4o-mini |
| `anthropic_llm` | Anthropic | Claude 4.x — long-context, prompt caching |
| `gemini_llm` | Google | gemini-2.5-flash / pro — generous free tier |
| `litellm_inference_asset` | Any | Provider-agnostic routing via LiteLLM |
| `ollama_inference_asset` | Local | $0, on-device |

Pick `gemini_llm` specifically when you want a **free** dev/test
experience or Google's long-context strength without bringing
LiteLLM along.
