# Document Summarization (agent_card + delegate, structured output)

**The simplest agent_card + delegate shape: one registered agent, forced structured output instead of free text.**

**Setup script:** [`setup_document_summarization_demo.sh`](./setup_document_summarization_demo.sh) — scaffolds a Dagster project, installs `agentic_pipeline` + `agent_card`, writes one registered agent + a 1-step pipeline. `bash setup_document_summarization_demo.sh` and `uv run dg dev`.

## What the demo shows

One `delegate` step picks `summarizer_agent` (`capabilities: [summarize]`) and asks it to summarize a sample document (a realistic incident postmortem). The agent -- an LLM call behind an MCP tool -- returns forced-structured JSON (`summary` + `action_items`), not free text, via tool-choice-forced function calling.

Total cost per run: **~$0.0005** (`gpt-4o-mini`, 2 LLM calls).

## Live-validated output

```json
{
  "summary": "On October 2, 2026, the checkout service experienced a significant latency increase due to a configuration change that improperly adjusted the connection pool size without updating the database's connection limits...",
  "action_items": [
    "Require performance review sign-off for any change that touches connection pool sizing, not just schema or code changes.",
    "Reduce the checkout-latency alerting window from 10 minutes to 3 minutes...",
    "Add a circuit breaker to the inventory-reservation client so pool saturation fails fast instead of queuing indefinitely.",
    "Schedule a load test of the new pool size against the downstream database's actual max_connections before the next capacity change."
  ]
}
```

Action items are extracted verbatim from the document's own numbered list -- not reworded, not hallucinated.

## When you'd reach for `reduce` instead

This demo's document fits in one call. For a document (or set of documents) too long for one context window, see the native `reduce` op (chunk + fold) instead of `delegate` -- `delegate` picks *who* handles something, `reduce` handles *how* to process something too big for one shot. The two compose: `reduce` to get a document down to a manageable summary, then `delegate` to a reviewer-persona agent for a final pass.

## Requirements

- `uv`, `OPENAI_API_KEY`
- Deps installed by the setup script: `litellm`, `requests`, `mcp>=1.0.0,<2`
