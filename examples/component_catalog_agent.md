# Component Catalog Agent — the catalog IS the curated set

**Component (new):** `ComponentCatalogAgentComponent` — fetches the LIVE manifest at runtime, presents a filtered slice to a planner LLM, and executes the picks via reflection + `dg.materialize()` in-process. **Real invocation, not simulation.**

**Script:** [`setup_component_catalog_agent_demo.sh`](./setup_component_catalog_agent_demo.sh)
**Cost:** ~$0.02 per run (planner + synthesis on gpt-4o-mini; the actual component executions are free — real Dagster asset materializations)
**Validated:** 2026-07-07 — RUN_SUCCESS end-to-end. Planner picked 2 real `synthetic_data_generator` invocations with the correct field names (thanks to runtime Pydantic introspection); both actually materialized real DataFrames (customers + products); synthesizer wrote a grounded description citing actual field names from the materialized data.

## Why this exists

The bounded-action-space idea from [Supervisor Agent](./supervisor_agent.md) and [MCP Tool Picker](./mcp_tool_picker.md) has a scaling problem: the tool list is hand-authored in YAML. When you have 900+ components in the registry, you can't hand-write each one as a tool spec.

**The insight: the manifest IS the curated set.** Filter it (by category, tags, or explicit IDs) and hand the filtered slice to the planner. That filter is the bounded action space. No hand-authoring.

Even better: since all component classes are already importable from `dagster_community_components` (that's the package the CLI installs), we can **execute picks for real** — no simulation, no subprocess trickery. Reflection-based instantiation via Pydantic, in-process materialization via `dg.materialize()`.

## Pipeline

```
catalog_plan       (planner: fetch manifest → filter → introspect fields → pick {id, config})
       ↓
catalog_execution  (for each pick: import class → instantiate → build_defs → materialize)
       ↓
catalog_answer     (synthesizer: grounded final answer citing real outputs)
```

## The key mechanic

At the executor step, this is what happens per planner pick:

```python
# Planner emits: {"id": "synthetic_data_generator", "config": {...}, "reason": "..."}
module = importlib.import_module("dagster_community_components")
cls = getattr(module, "SyntheticDataGeneratorComponent")   # class name inferred from id
instance = cls(**config)                                    # Pydantic validates + instantiates
defs = instance.build_defs(context)                         # get Dagster Definitions
assets = list(defs.assets or [])
result = dg.materialize(assets)                             # REAL in-process materialization
value = result.output_for_node(asset_name)                  # actual DataFrame
```

Every step is real. No fake. The planner picks REAL components; the executor runs REAL Dagster asset materializations; the synthesizer describes REAL outputs.

## Field introspection — how the planner picks correct configs

An early attempt failed because the planner emitted `num_rows: 10` when the actual field is `row_count`. The fix: at build time, the component reads each catalog entry's Pydantic class and inspects `model_fields`. The planner sees:

```
- id: synthetic_data_generator | Component to generate synthetic data...
    required=['asset_name', 'schema_type'], optional_examples=['row_count', 'random_state', ...]
```

Now the planner emits the actual field names. This is why the demo succeeds where earlier attempts would have failed — the schema is fetched dynamically from the real class.

## Prerequisites

- `uv` + `OPENAI_API_KEY` + internet (fetches manifest.json)

## Run

```bash
export OPENAI_API_KEY=sk-...
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_component_catalog_agent_demo.sh -o setup_component_catalog_agent_demo.sh
chmod +x setup_component_catalog_agent_demo.sh
./setup_component_catalog_agent_demo.sh
```

## Validated run output (2026-07-07)

**Planner's picks:**

```
id                        config                                                       reason
synthetic_data_generator  {"asset_name": "customers_dataset", "schema_type": "customers", "row_count": 10}  Generate customers dataset for testing
synthetic_data_generator  {"asset_name": "products_dataset",  "schema_type": "products",  "row_count": 10}  Generate products dataset for testing
```

**Real execution outputs (excerpt):**

```
customer_id  first_name  last_name  email                          city         signup_date  lifetime_value  is_active
CUST000001   John        Rodriguez  john.rodriguez372@example.com  Los Angeles  2026-06-20   327.92          True
CUST000002   Emma        Smith      emma.smith468@example.com      Los Angeles  2025-11-09   3070.46         False
...

product_id   name              category     price   cost    margin_pct  stock_quantity  rating  is_available
PROD000001   Premium Bundle    Food         447.28  207.54  53.6        230             4.7     True
PROD000002   Classic Set       Food         205.13  101.93  50.3        150             3.2     False
...
```

**Synthesizer's answer (grounded in real data):**

> Two synthetic datasets have been successfully generated: one for customers and another for products.
>
> The **customers dataset** contains: `customer_id`, `first_name`, `last_name`, `email`, `phone`, `city`, `state`, `signup_date`, `lifetime_value`, `is_active`. 10 rows of customer information with personal contact, location, signup date, lifetime value, and active status.
>
> The **products dataset** contains: `product_id`, `name`, `category`, `price`, `cost`, `margin_pct`, `stock_quantity`, `rating`, `num_reviews`, `is_available`. 10 rows detailing product identifiers, categories, pricing, stock levels, ratings, and availability.

The synthesizer cites REAL field names from the REAL materialized DataFrames.

## Bounding the catalog

The `include_*` filters are the bounded action space. In production, scope tight:

```yaml
# Just synthetic-data components:
include_ids: [synthetic_data_generator, image_generation, ...]

# Or a whole category:
include_categories: [ai]              # ~40 AI components
include_categories: [transformation]  # dozens of transforms

# Or by tags:
include_tags: [transform, filter]
```

`max_catalog_entries` (default 40) caps how many entries reach the planner regardless of filter breadth — the manifest has 900+ entries and sending them all to gpt-4o-mini would blow the context.

## Limitations (v1)

- **Source-style components work best.** The planner should pick components with no external upstream deps (`synthetic_data_generator`, `text_embedding_asset`, `langchain_chain_asset` with a static task, etc.). Components expecting `upstream_asset_key` have no upstream at runtime.
- **Resource-requiring components fail gracefully.** Snowflake / S3 / Slack / etc. components can't run without their resources. `fail_on_execution_error: false` (default) logs and continues. Set to `true` for strict mode.
- **Multi-asset components get one output captured.** Fine for `synthetic_data_generator` (1 asset). For components like `supervisor_agent` (N assets), the executor grabs the last one — imperfect but works for the demo.

## Extension patterns

- **`langchain_chain_asset` in the catalog.** Set `include_ids: [langchain_chain_asset, synthetic_data_generator]`. Planner can now generate synthetic data AND run an LLM over the row-wise output — genuinely chained real components.
- **Iterative catalog agent.** Combine with [Iterative Supervisor Agent's](./iterative_supervisor_agent.md) shape — planner runs once per step, sees the executed output from the last step, picks the next component. That gives you a truly-real iterative agent with per-step lineage over the whole registry.
- **Human-in-the-loop.** Gate `catalog_plan` on an `asset_check` that fails if the planner picked any component tagged `destructive`. Manual approval before `catalog_execution` runs.
- **Cost cap.** Track per-execution wall-clock; abort further picks if the run has already exceeded a budget.

## The full family of agentic-pipeline demos

Component Catalog Agent is #8. Common thread: **agent picks by name from a bounded set. Dagster executes.**

1. [Data Doctor](./data_doctor.md) — pick column REMEDIATIONS
2. [Adaptive Triage Router](./adaptive_triage.md) — pick per-row DOWNSTREAM ROUTE
3. [Adaptive Backfill Detective](./adaptive_backfill.md) — pick per-partition FILL STRATEGY
4. [Supervisor Agent](./supervisor_agent.md) — pick WHICH SPECIALISTS (hand-declared)
5. [MCP Tool Picker](./mcp_tool_picker.md) — pick WHICH MCP TOOLS
6. [Adaptive Research Brief](./adaptive_research_brief.md) — pick HOW MANY items
7. [Iterative Supervisor Agent](./iterative_supervisor_agent.md) — pick tools ITERATIVELY (chained)
8. **Component Catalog Agent** *(this demo)* — pick from the LIVE REGISTRY, execute REAL

Each earlier demo either has a hand-authored tool set or invokes LLM personas. This one is the only demo where the "tool set" is *the entire component registry* and every pick is a *real Dagster component materialization*. That's the pattern that scales.

## Related

- [Supervisor Agent](./supervisor_agent.md) — single-shot, hand-authored tool set. Simpler; use when the catalog you'd hand-author is small.
- [Iterative Supervisor Agent](./iterative_supervisor_agent.md) — chained tool use with per-step visibility. Combines naturally with this shape for iterative catalog picking.
- [MCP Tool Picker](./mcp_tool_picker.md) — MCP-backed tools instead of registry components.
