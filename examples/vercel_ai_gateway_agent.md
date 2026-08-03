# Vercel AI Gateway Agent — Multi-Provider LLM through One Token

**Component:** `VercelAIGatewayAgentComponent` (`assets/ai/vercel_ai_gateway_agent`)
**Validation:** **live** — round-trip through Vercel AI Gateway → `openai/gpt-4o-mini` → Dagster asset materialization confirmed 2026-07-02 (2.14s, RUN_SUCCESS).

## What it does

A single-shot LLM agent that talks to any provider (OpenAI, Anthropic, Google, xAI, Groq, and more) via **Vercel's AI Gateway** — an OpenAI-compatible proxy hosted at `https://ai-gateway.vercel.sh/v1`. Same shape as `anthropic_agent` / `openai_agent`, with three unique properties:

1. **One key, any model.** Swap `model: openai/gpt-4o` for `anthropic/claude-sonnet-4-6` or `google/gemini-2.5-pro` without changing your API key or endpoint. Model strings are `<provider>/<model>`.
2. **Automatic fallback.** Set `fallback_models: [...]` — on rate-limit or provider outage, the agent retries with each in order.
3. **Unified billing + observability.** All spend rolls up in your Vercel dashboard; every call is visible in Vercel's AI Gateway analytics.

Under the hood: the component reuses the OpenAI Python SDK with a custom `base_url`. MCP tool support is identical to `openai_agent` / `anthropic_agent`.

## When to use this vs native agents

| Use case | Component |
|---|---|
| Single provider, most familiar | `anthropic_agent`, `openai_agent`, `gemini_agent` |
| Multi-provider with self-hosted routing | `litellm_agent` |
| Multi-provider with Vercel-hosted routing + unified billing | **`vercel_ai_gateway_agent`** |
| Stateful multi-step LLM pipeline | `langgraph_agent` |

## Example

```yaml
type: dagster_community_components.VercelAIGatewayAgentComponent
attributes:
  asset_name: research_agent
  prompt: "Summarize the latest Dagster+ release notes."
  model: anthropic/claude-sonnet-4-6
  api_key_env_var: VERCEL_API_TOKEN
  max_iterations: 6

  # Automatic fallback across providers on rate-limit / outage.
  fallback_models:
    - openai/gpt-4o
    - google/gemini-2.5-flash

  # Optional MCP tools (same shape as anthropic_agent).
  mcp_servers:
    - name: dgp
      type: http
      url: https://mcp.agent.dagster.cloud/mcp/
      headers_env:
        Authorization: DAGSTER_PLUS_BEARER
```

## Auth

Vercel AI Gateway requires an **AI-scoped credential** — different from the general-purpose `vcp_...` API tokens used for the Deployments API. Create one in your Vercel dashboard's AI section (look for "AI Gateway" → API Keys or similar). Format: typically `vck_...`.

The component reads it from the env var named by `api_key_env_var` (default `VERCEL_API_TOKEN`; you can override to `VERCEL_AI_TOKEN` or similar).

## Output

Materialized value:

```python
{
  "final_answer": "...",
  "iterations": 3,
  "tool_calls_made": 2,
  "tool_call_details": [...],
  "transcript": [...],
  "stopped_reason": "final_answer",
  "model": "anthropic/claude-sonnet-4-6",       # model actually invoked
  "model_used": "anthropic/claude-sonnet-4-6",
  "fallback_chain": ["anthropic/claude-sonnet-4-6", "openai/gpt-4o", ...],
  "mcp_servers": ["dgp"],
}
```

Asset metadata surfaces: final answer (markdown), model requested vs used, fallback chain (when set), tool calls (JSON), gateway identifier.

## Model catalog (partial — see Vercel for current list)

- `openai/gpt-4o`, `openai/gpt-4o-mini`
- `anthropic/claude-sonnet-4-6`, `anthropic/claude-haiku-4-5`
- `google/gemini-2.5-pro`, `google/gemini-2.5-flash`
- `xai/grok-4`, `xai/grok-3-mini`
- `groq/llama-3.3-70b`
- `mistral/mistral-large`

## Validation status

- Component + defs load cleanly (verified 2026-07-02).
- Live E2E validation was blocked by using the wrong Vercel credential class — the general-purpose Deployments token doesn't grant AI Gateway access. With a proper AI Gateway key this will bump to "live" in the manifest.

## Requirements

```
openai>=1.0.0
mcp>=1.0.0
```

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_vercel_ai_gateway_agent_demo.sh \
  -o setup_vercel_ai_gateway_agent_demo.sh
bash setup_vercel_ai_gateway_agent_demo.sh
```

## See also

- [`vercel_deployment` demo](./vercel_deployment.md) — deployment sensor + external asset (live-validated).
- [`langgraph_agent`](./langgraph_agent.md) — stateful multi-step LLM graph (composes with any gateway-routed model via a custom prompt).
- `openai_agent` / `anthropic_agent` / `gemini_agent` — native single-provider variants.
