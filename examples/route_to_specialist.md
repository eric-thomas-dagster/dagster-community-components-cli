# Route to a Specialist (delegate, semantic picking)

**Three registered specialist agents, NO `required_capabilities` set at all -- proves the picker LLM does genuine semantic matching, not just tag filtering.**

**Setup script:** [`setup_route_to_specialist_demo.sh`](./setup_route_to_specialist_demo.sh) — scaffolds a Dagster project, installs `agentic_pipeline` + `agent_card`, writes three registered specialist agents + a 1-step pipeline. `bash setup_route_to_specialist_demo.sh` and `uv run dg dev`.

## What the demo shows

One `delegate` step reads a customer question and picks among `billing_specialist`, `technical_specialist`, and `general_specialist` -- deliberately with **no capability tags to pre-filter on**. The picker LLM reads each candidate's `skills`/`description` and judges fit from the question's actual content.

Total cost per run: **~$0.0005** (`gpt-4o-mini`, 2 LLM calls).

## Live-validated output -- three real questions, three real picks

Same pipeline, same three registered agents, only `source.text` changes:

- *"I was charged twice for my subscription this month, can you help me get a refund for the extra charge?"* → picked `billing_specialist` -- *"This agent specializes in billing issues, including refunds and subscription charges."*
- *"Your API keeps returning a 500 error when I POST to /v2/orders with more than 10 items in the payload."* → picked `technical_specialist` -- *"This question involves a technical error related to the API, making the technical specialist the best fit."*
- *"What's the difference between the free plan and the pro plan?"* → picked `general_specialist` -- *"The question is about general product information regarding the differences between plans."*

Each drafted response is grounded in the specific question's content (the technical one references the real `500 error`/`/v2/orders` details), not generic boilerplate.

## `route` vs `delegate` -- when to use which

The native `route` op does this exact pattern too -- a router LLM picks from an inline `specialists:` list. Use `route` when your specialist roster is small, fixed, and known at pipeline-write time. Use `delegate` + `agent_card` when the roster is shared across multiple pipelines, or grown by someone other than whoever writes this particular pipeline -- add a fourth specialist by dropping in one more `AgentCardComponent`, zero edits to the pipeline YAML.

## Requirements

- `uv`, `OPENAI_API_KEY`
- Deps installed by the setup script: `litellm`, `requests`, `mcp>=1.0.0,<2`
