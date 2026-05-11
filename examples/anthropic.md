# Anthropic Claude — anthropic_llm component on synthetic tickets

**Validated at infra level** — script + defs.yaml load clean
(`dg check defs` passes); end-to-end run requires `ANTHROPIC_API_KEY`.

```
support_tickets (synthetic 20 rows from synthetic_data_generator)
       │
       └── ticket_summaries  ← anthropic_llm (claude-haiku-4-5-20251001)
```

## Components covered (1)

| Component | What it does |
|---|---|
| `anthropic_llm` | Run Claude on a DataFrame column. Supports prompt caching, batching, all current Claude models. Same `input_column` / `output_column` shape as the other AI components. |

## Cost

~**$0.01–$0.05** for 20 rows against Claude Haiku 3.5 (the cheapest +
fastest current model). Bigger models are pricier:

| Model | Input \$ / 1M tok | Output \$ / 1M tok |
|---|---|---|
| `claude-haiku-4-5-20251001` | $0.80 | $4 |
| `claude-sonnet-4-6` | $3 | $15 |
| `claude-opus-4-7` | $15 | $75 |

## Required env var

```bash
ANTHROPIC_API_KEY=sk-ant-...
```

## Run it

```bash
./setup_anthropic_demo.sh
cd anthropic-demo
uv run dg launch --assets '*'
```

## Switch models

Edit `src/<pkg>/defs/anthropic_llm/defs.yaml` and change `model:`:

```yaml
model: claude-sonnet-4-6   # better at long-context analysis
# or
model: claude-opus-4-7       # most capable, slowest
```

## Why both `openai_llm` and `anthropic_llm`?

The registry exposes both because production deployments often
multi-vendor for resilience and cost. Same shape, different provider:

| Component | Provider | Equivalent purpose |
|---|---|---|
| `openai_llm` | OpenAI | gpt-4o, gpt-4o-mini |
| `anthropic_llm` | Anthropic | Claude 3.5 family |
| `litellm_inference_asset` | Either via LiteLLM | Provider-agnostic routing |
| `ollama_inference_asset` | Local Ollama | $0 cost, on-device |

Pick anthropic_llm specifically when you want Claude's tool-use,
prompt-caching (90% off on repeated context), or 200k-token context
window.
