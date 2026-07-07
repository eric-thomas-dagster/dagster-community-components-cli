# Iterative Catalog Agent — schema-discovering chained pipeline over 900 real components

**Component (new):** `IterativeCatalogAgentComponent` — fusion of `iterative_supervisor_agent` (per-step planner, N assets, short-circuit) + `component_catalog_agent` (live manifest, reflection, real materialization). At each step the planner sees the ACTUAL columns of prior step outputs, so it can plan against data with unknown schemas.

**Script:** [`setup_iterative_catalog_agent_demo.sh`](./setup_iterative_catalog_agent_demo.sh)
**Cost:** ~$0.02 per run (N planner calls + 1 synthesis, all gpt-4o-mini)
**Validated:** 2026-07-07 — 3-step real chained pipeline built via schema discovery. Task did NOT tell the agent what columns exist; the agent discovered `is_active`, `city`, `state`, `lifetime_value` from step 1's actual output.

## Why this is the strongest agentic shape

Every earlier agentic demo has a schema-knowledge gap:

- [Supervisor Agent](./supervisor_agent.md) — planner picks tools upfront, no output introspection.
- [Iterative Supervisor Agent](./iterative_supervisor_agent.md) — iterative, but tools are LLM personas (roleplay), no real column info.
- [Component Catalog Agent](./component_catalog_agent.md) — real components, but single-shot planning; downstream picks have to GUESS the upstream column names (had to spell them out in the task).

Iterative Catalog Agent solves the schema problem. The planner at step 2+ sees:

```
Prior steps:
Step 1:
  picked: synthetic_data_generator
  config: {schema_type: "customers", row_count: 100, ...}
  produced asset: synthetic_data
  columns: ['customer_id', 'first_name', 'last_name', 'email', 'phone', 'city',
            'state', 'signup_date', 'lifetime_value', 'is_active']
  preview:
    | customer_id | first_name | last_name | email                       | ...
    | CUST000001  | John       | Rodriguez | john.rodriguez@example.com  | ...
```

Now the planner picks step 2 knowing the REAL column names. No guessing.

## Pipeline

```
catalog_step_1     (planner picks 1st component from manifest; executor materializes;
                    captures real output columns + preview)
       ↓
catalog_step_2     (planner sees step 1's REAL columns; picks next component;
                    executor wires prior step's DataFrame as upstream source)
       ↓
catalog_step_3     (same again, with step 1+2 output info)
       ↓
...
catalog_step_N     (whichever step declares done short-circuits later steps)
       ↓
catalog_final_answer  (synthesizer reads full trajectory → grounded answer)
```

## The chaining mechanic

When a step's picked component uses `upstream_asset_key`:
1. Executor looks up prior steps' outputs by matching `asset_name`.
2. Constructs an **ad-hoc source asset** seeded from the prior step's actual DataFrame value.
3. Calls `dg.materialize(source + picked_component_assets)` — Dagster runs both in one graph, and the picked component reads REAL data via `upstream_asset_key`.

## Prerequisites

- `uv` + `OPENAI_API_KEY` + internet (fetches manifest.json)

## Run

```bash
export OPENAI_API_KEY=sk-...
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_iterative_catalog_agent_demo.sh -o setup_iterative_catalog_agent_demo.sh
chmod +x setup_iterative_catalog_agent_demo.sh
./setup_iterative_catalog_agent_demo.sh
```

## Validated run output (2026-07-07)

**The task deliberately did NOT tell the agent what columns exist:**

> *Step A: Generate 100 rows of synthetic data (pick any interesting schema).
> Step B: Once you see the actual columns produced, filter to some interesting
> subset using real column names you now know exist.
> Step C: Summarize the filtered set using real columns.
> Then declare done.*

**Real trajectory:**

```
step 1: synthetic_data_generator {schema_type: "customers", row_count: 100}
        → asset: synthetic_data
        → REAL columns: [customer_id, first_name, last_name, email, phone,
                         city, state, signup_date, lifetime_value, is_active]

step 2: filter {condition: "is_active == True",
                upstream_asset_key: "synthetic_data"}
        → asset: filtered_customers
        → uses REAL column `is_active` DISCOVERED from step 1

step 3: summarize {group_by: ["city", "state"],
                   aggregations: {"average_lifetime_value": {col: "lifetime_value", agg: "avg"},
                                  "row_count": {col: "lifetime_value", agg: "count"}},
                   upstream_asset_key: "filtered_customers"}
        → asset: customer_summary
        → uses REAL columns `city`, `state`, `lifetime_value` from step 2
        → output columns: [city, state, row_count, average_lifetime_value]

step 4: DONE ("workflow complete — summary of active customers by city and state")
step 5: short-circuit (prior step done)
```

**The proof of schema discovery**: the planner picked `condition: "is_active == True"` in step 2. Nowhere in the task or config did we tell it about an `is_active` column. It learned it from step 1's real output.

## What this unlocks

- **Customer-built data with unknown schemas.** Point step 1 at `snowflake_query_asset` / `bigquery_query_asset` / `dataframe_from_csv` and the agent discovers your actual columns at runtime.
- **Domain-specific pipelines.** Give the agent a filtered catalog (`include_tags: [transform, ml]`) and a business task; it builds the right pipeline for your data.
- **Full auditability.** Every step's picked component + config + real output columns is a Dagster asset in `dg dev`. No hidden state.

## Limitations (v1)

- **Serial materialization.** Each step calls `dg.materialize()` in-process. Total wall-clock scales with `N × per_pick_materialize_time`.
- **In-memory data hand-off.** The ad-hoc source asset holds the prior step's DataFrame in memory. Fine for demo-scale data; large DataFrames would need a different strategy (spill to disk, or restructure so prior step writes to a well-known location that downstream reads via a source component).
- **Resource-requiring components fail gracefully.** Snowflake / S3 / Slack / etc. components can't run without their resources; `fail_on_execution_error: false` lets the run continue.
- **Only components that produce a DataFrame chain well.** Non-DataFrame outputs (dicts, strings, etc.) won't wire as `upstream_asset_key` sources cleanly.

## The full agentic-pipeline family

Iterative Catalog Agent is #9 — the current apex.

1. [Data Doctor](./data_doctor.md) — pick column REMEDIATIONS
2. [Adaptive Triage Router](./adaptive_triage.md) — pick per-row DOWNSTREAM ROUTE
3. [Adaptive Backfill Detective](./adaptive_backfill.md) — pick per-partition FILL STRATEGY
4. [Supervisor Agent](./supervisor_agent.md) — pick WHICH SPECIALISTS (hand-authored, LLM personas)
5. [MCP Tool Picker](./mcp_tool_picker.md) — pick WHICH MCP TOOLS
6. [Adaptive Research Brief](./adaptive_research_brief.md) — pick HOW MANY items
7. [Iterative Supervisor Agent](./iterative_supervisor_agent.md) — pick tools ITERATIVELY (chained, LLM personas)
8. [Component Catalog Agent](./component_catalog_agent.md) — pick from LIVE REGISTRY, execute REAL (single-shot)
9. **Iterative Catalog Agent** *(this demo)* — pick from LIVE REGISTRY, execute REAL, WITH schema discovery (iterative)

The progression: hand-authored → catalog-driven → real invocation → iterative → schema-aware. Each step handles a real limitation of the prior shape.

## Related

- [Component Catalog Agent](./component_catalog_agent.md) — single-shot version. Simpler; use when you know upstream schemas.
- [Iterative Supervisor Agent](./iterative_supervisor_agent.md) — same iterative shape but LLM-persona tools.
- [MCP Tool Picker](./mcp_tool_picker.md) — MCP-backed real tools; single-shot.
