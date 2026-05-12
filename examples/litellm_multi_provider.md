# LiteLLM Multi-Provider — same prompt, multiple vendors side by side

**Validated end-to-end against real APIs** (Gemini + OpenAI in this session;
Anthropic auto-included when ANTHROPIC_API_KEY is in env). Runs the same
prompt through every provider you have a key for, joins responses
side-by-side for cost / quality / latency comparison.

```
support_tickets             (3 synthetic tickets)
       │
       ├── classified_gemini      ← litellm_inference_asset (gemini-2.5-flash)
       ├── classified_openai      ← litellm_inference_asset (gpt-4o-mini)
       └── classified_anthropic   ← litellm_inference_asset (claude-haiku-4-5)
                                  │
                  └── multi_provider_compare    ← pandas (join on ticket_id) → CSV
```

Each provider gets its own Dagster asset (so you see them as parallel
nodes in the graph), backed by the **single** `litellm_inference_asset`
component — same shape, different `model:` + `api_key_env_var:`.

## Components covered (1, exercised across N providers)

| Component | What it does |
|---|---|
| `litellm_inference_asset` | Per-row LLM inference via LiteLLM. Routes to 100+ providers (OpenAI, Anthropic, Google, Bedrock, Azure, Groq, OpenRouter, Together, Fireworks, etc.) through one component — change `model:` and `api_key_env_var:` to switch. |

## Validation status — live

Real run output (Gemini + OpenAI both ran in ~9s, see CSV):

| ticket | gemini-2.5-flash | gpt-4o-mini |
|---|---|---|
| Charge my card ending in 9685. | Request to charge card ending in 9685. | Request to charge credit card ending in 9685. |
| Site is down — 502 errors since 9am EST. | User reports site down with 502 errors since 9 AM EST. | User reports 502 errors and site downtime since 9am EST. |
| API rate limit too low for production. | API rate limit is insufficient for production use. | User requests an increase in API rate limit for production use. |

## Cost

**~$0.001 per provider.** 3 short tickets × cheapest tier of each
vendor. Gemini-2.5-flash has 20 req/min free; gpt-4o-mini is
~$0.00015 per ticket at this length.

## Required env vars (provide as many as you have)

```bash
export GEMINI_API_KEY=...
export OPENAI_API_KEY=sk-...
export ANTHROPIC_API_KEY=sk-ant-...
```

The script auto-detects which keys are set and only creates the
corresponding `classified_*` assets — works with 1, 2, or 3 providers.

## Run it

```bash
./setup_litellm_multi_provider_demo.sh
cd litellm-multi-provider-demo
uv run dg launch --assets '*'
cat /tmp/litellm_multi_provider.csv
```

## Why LiteLLM vs the native components?

| Pattern | Components | Best for |
|---|---|---|
| **Multi-vendor / model-switching** | `litellm_inference_asset`, `litellm_embedding_batch`, `litellm_image_generation` | A/B tests, fallback strategies, multi-vendor resilience, comparison demos like this one |
| **Single-vendor / deepest features** | `openai_llm` / `anthropic_llm` / `gemini_llm` | Production shops that standardize on one provider and want vendor-specific features (prompt caching, thinking_budget, structured outputs, etc.) |

You can mix: use native components for production paths, LiteLLM for
the cost/quality comparison harness behind the scenes.
