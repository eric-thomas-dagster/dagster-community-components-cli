# Cube + LLM — natural-language narrative over governed metrics
> ❌ **Dagster+ Serverless:** local-only demo — requires container/server dependency.

**Components:** `CubeQueryAssetComponent`, `LangChainChainAssetComponent`

**Script:** [`setup_cube_llm_demo.sh`](./setup_cube_llm_demo.sh)
**Cost:** ~$0.01 per run (a few gpt-4o-mini calls)
**Validated:** 2026-07-06 — Cube query + LangChain narrative asset materialize end-to-end against a local Cube dev server.

## Why this exists

Everyone wants "ask questions of your data in English." The obvious approach — LLM writes SQL, warehouse runs it — has a well-known failure mode: LLMs hallucinate columns, get joins subtly wrong, and can write unsafe queries. The safer shape is **Cube as the interface**:

- **LLM never touches SQL.** It sees only Cube's semantic layer (`/v1/meta`) — a curated list of measures + dimensions with human-readable descriptions.
- **Cube enforces governance.** Every metric is defined in the Cube model. The LLM can't invent columns.
- **The LLM's job shrinks** from "generate valid SQL" to "narrate typed structured results" — dramatically more reliable.

```
Cube semantic layer (localhost:4000, sample Orders cube — 12 rows)
        ↓
cube_customer_totals   ← CubeQueryAssetComponent (totalAmount + count per customer)
        ↓
cube_narrative_answer  ← LangChainChainAssetComponent (gpt-4o-mini per row)
```

## Prerequisites

- `uv` — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- `docker` — Docker Desktop or engine (Cube runs locally)
- `OPENAI_API_KEY`

## Run

```bash
export OPENAI_API_KEY=sk-...
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_cube_llm_demo.sh -o setup_cube_llm_demo.sh
chmod +x setup_cube_llm_demo.sh
./setup_cube_llm_demo.sh
```

## What the script does

1. Spins up `cubejs/cube:latest` via Docker with a sample `Orders` cube. Each measure and dimension has a description — this is the LLM's grounding context.
2. Prints Cube's `/v1/meta` so you can see exactly what the LLM's schema view looks like.
3. Scaffolds a Dagster project and installs `dagster-community-components` + `langchain-openai` + `requests`.
4. Writes two `defs.yaml`:
   - `cube_customer_totals` — `CubeQueryAssetComponent` querying `Orders.totalAmount` + `Orders.count` grouped by `Orders.customerName`, sorted desc.
   - `cube_narrative_answer` — `LangChainChainAssetComponent` calling gpt-4o-mini once per row, generating a natural-language sentence with the exact numbers.
5. Materializes both together (`--select 'cube_customer_totals+'`) so the LLM asset sees the Cube result in the same run.

## The `strip_cube_prefix` field

Cube returns column names like `Orders.customerName`. Fine for JSON but breaks LangChain's `{var}` template syntax (dots aren't valid identifiers). `CubeQueryAssetComponent` has `strip_cube_prefix: true` **on by default** — columns arrive as `customerName` / `totalAmount` / `count`, which is what the LangChain `prompt_template` expects.

## Extending to full text-to-Cube-query

The demo hard-codes the Cube query. For the full NL flow:

1. Add an upstream asset that fetches `/v1/meta` and passes it as context to a `langgraph_agent`.
2. The agent prompt: *"Here's the Cube schema. Convert this English question into a Cube JSON query."*
3. Feed the agent's output into `CubeQueryAssetComponent`'s `query` field (needs dynamic-query component support — the current `query` is static YAML).

The primitives are all here — the LLM demo proves the safety-layer pattern. Wiring dynamic query generation is one component away.

## See also

- [Cube semantic layer — simple + LLM demos](./cube_query.md) — the parent walkthrough covering both the no-LLM and LLM demos.
- [LangGraph Agent](./langgraph_agent.md) — the natural fit for the text-to-Cube-query step above.
- [dbt + LLM + dbt (mid-DAG generative)](./dbt_llm_pipeline.md) — same row-wise LLM pattern, but over dbt marts instead of Cube results.
