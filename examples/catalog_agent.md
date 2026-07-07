# Catalog Agent — schema-discovering chained pipeline over 900 real components

**Component (new):** `CatalogAgentComponent` — the most sophisticated agentic primitive in the registry. Per-step planner picks REAL components from the live 900-component manifest, executes them via reflection + in-process materialization, and sees the ACTUAL columns of each step's output before planning the next — so it can plan against data with unknown schemas.

**Script:** [`setup_catalog_agent_demo.sh`](./setup_catalog_agent_demo.sh)
**Cost:** ~$0.02 per run (N planner calls + 1 synthesis, all gpt-4o-mini)
**Validated:** 2026-07-07 — 3-step real chained pipeline built via schema discovery. Task did NOT tell the agent what columns exist; the agent discovered `is_active`, `city`, `state`, `lifetime_value` from step 1's actual output.

## Why this is the strongest agentic shape

Every earlier agentic demo has one of two gaps:

- [Supervisor Agent](./supervisor_agent.md) / [Iterative Supervisor Agent](./iterative_supervisor_agent.md) — tools are LLM personas (roleplay). No REAL execution.
- Any hand-authored tool list — doesn't scale to 900 components in the registry.

Catalog Agent closes both gaps. The tools are the ENTIRE registry (filtered). Every pick is a REAL component materialization. The planner at step 2+ sees:

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
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_catalog_agent_demo.sh -o setup_catalog_agent_demo.sh
chmod +x setup_catalog_agent_demo.sh
./setup_catalog_agent_demo.sh
```

## Validated run output (2026-07-07)

### Simple schema-discovery task

**Task:** *"Generate synthetic data (any schema), then filter to an interesting subset using real columns, then summarize."*

```
step 1: synthetic_data_generator {schema_type: "customers", row_count: 100}
        → REAL columns discovered: [customer_id, first_name, ..., is_active]

step 2: filter {condition: "is_active == True", upstream_asset_key: "synthetic_data"}
        → uses REAL column `is_active` DISCOVERED from step 1

step 3: summarize {group_by: ["city", "state"],
                   aggregations: {"average_lifetime_value": {col: "lifetime_value", agg: "avg"}}}
        → uses REAL columns from step 2

step 4: DONE
```

### Multi-source join task with self-correction

**Task (as a real user would write it, no step-by-step):** *"Generate synthetic orders and synthetic customers, join them, group by first name, email, and month, sum total and count of orders, and store to a csv."*

Notice the planner made a mistake and self-corrected:

```
step 1: synthetic_data_generator (customers)   → asset: customers ✓
step 2: dataframe_join (left_asset_key=orders, right_asset_key=customers)
        → FAILED — orders doesn't exist yet
        → error surfaced to next step's planner via prior-steps summary
step 3: synthetic_data_generator (orders)      → asset: orders ✓  (course-correct!)
step 4: dataframe_join (left_asset_key=orders, right_asset_key=customers)  ✓
        → REAL multi-source wiring; picks first_name/email/etc. into the joined DataFrame
step 5: formula {expressions: {"month": "order_date.dt.month"}}  ✓
step 6: summarize {group_by: ["first_name", "email", "month"],
                   aggregations: {total: sum, order_count: count}}  ✓
        → uses REAL columns from the JOINED DataFrame (first_name/email from customers side)
step 7: dataframe_to_csv → /tmp/orders_by_customer_month.csv  ✓
step 8: DONE
```

Real CSV written:
```
first_name,email,month,total,order_count
Emily,emily.jones82@example.com,6,442.85,1
Jane,jane.davis836@example.com,6,128.12,1
John,john.rodriguez857@example.com,7,639.17,1
...
```

**What made this work:**
- Multi-source wiring — both `left_asset_key` and `right_asset_key` resolved to prior assets.
- Dangling-upstream validation — step 2's planning error surfaced to step 3's planner.
- Course-correction — step 3 picked the missing source (orders) instead of hallucinating again.

**The proof of schema discovery**: the planner picked `condition: "is_active == True"` in step 2. Nowhere in the task or config did we tell it about an `is_active` column. It learned it from step 1's real output.

## What this unlocks

- **Customer-built data with unknown schemas.** Point step 1 at `snowflake_query_asset` / `bigquery_query_asset` / `dataframe_from_csv` and the agent discovers your actual columns at runtime.
- **Domain-specific pipelines.** Give the agent a filtered catalog (`include_tags: [transform, ml]`) and a business task; it builds the right pipeline for your data.
- **Full auditability.** Every step's picked component + config + real output columns is a Dagster asset in `dg dev`. No hidden state.

## Graduation path — turn a successful run into permanent Dagster assets

Running the agent every time costs LLM tokens. Once the agent discovers a pipeline you like, GRADUATE — generate real defs.yaml files, commit them, drop the agent config:

```yaml
type: dagster_community_components.CatalogAgentComponent
attributes:
  # ... task, include_categories, etc ...
  codegen_output_dir: src/my_project/defs   # ← add this
```

After the next successful run, one `defs.yaml` per successful pick lands at `src/my_project/defs/<asset_name>/defs.yaml`:

```yaml
# generated by CatalogAgentComponent — synthetic_orders
type: dagster_community_components.SyntheticDataGeneratorComponent
attributes:
  asset_name: "synthetic_orders"
  schema_type: "orders"
  row_count: 300
```

```yaml
# generated by CatalogAgentComponent — orders_with_customers
type: dagster_community_components.DataframeJoin
attributes:
  asset_name: "orders_with_customers"
  left_asset_key: "synthetic_orders"
  right_asset_key: "synthetic_customers"
  how: "inner"
```

Once committed:
1. Remove the `CatalogAgentComponent` config from your defs folder.
2. The generated defs.yaml files reference each other via `upstream_asset_key` / `left_asset_key` / etc. — Dagster wires them normally.
3. `dg check defs` → clean. `dagster asset materialize --select '*'` → runs the pipeline **with zero LLM cost**.

Live-validated: 5 emitted defs.yaml files → fresh project → `dg check defs` clean → materialize succeeded in ~7s (vs ~50s with the agent + LLM calls). Real CSV on disk.

Even without `codegen_output_dir` set, the synthesis asset's metadata includes a `codegen` markdown preview showing the equivalent YAML for each pick — you can copy-paste from `dg dev`.

## What it handles

- **Multi-source picks** — components with `left_asset_key` + `right_asset_key` (e.g. `dataframe_join`) or `additional_asset_keys` (N-way joins) wire correctly. Any config field ending in `_asset_key` / `_asset_keys` referencing a prior step's asset gets a real source-asset injection.
- **Self-correction on planning errors** — if the planner references an upstream that doesn't exist yet, the step fails with a specific error, and the NEXT step's planner sees the error in its prior-steps summary and course-corrects (e.g. picks the missing source component).
- **Class-name inference variants** — most classes end in `Component` (`SyntheticDataGeneratorComponent`), some don't (`DataframeJoin`). The catalog resolver tries both patterns.

## Limitations (v1)

- **Serial materialization.** Each step calls `dg.materialize()` in-process. Total wall-clock scales with N. For parallel execution, batch independent source components into a single step (needs component-level fan-out support).
- **In-memory data hand-off.** The ad-hoc source asset holds the prior step's DataFrame in memory. Fine for demo-scale data; very large DataFrames would need spill-to-disk.
- **Resource-requiring components fail gracefully.** Snowflake / S3 / Slack / etc. components can't run without their resources; `fail_on_execution_error: false` lets the run continue.
- **Only components that produce a DataFrame chain well.** Non-DataFrame outputs (dicts, strings, etc.) won't wire as source-asset upstreams cleanly.

## The full agentic-pipeline family

Catalog Agent is the current apex.

1. [Data Doctor](./data_doctor.md) — pick column REMEDIATIONS
2. [Adaptive Triage Router](./adaptive_triage.md) — pick per-row DOWNSTREAM ROUTE
3. [Adaptive Backfill Detective](./adaptive_backfill.md) — pick per-partition FILL STRATEGY
4. [Supervisor Agent](./supervisor_agent.md) — pick WHICH SPECIALISTS (hand-authored, LLM personas)
5. [MCP Tool Picker](./mcp_tool_picker.md) — pick WHICH MCP TOOLS
6. [Adaptive Research Brief](./adaptive_research_brief.md) — pick HOW MANY items
7. [Iterative Supervisor Agent](./iterative_supervisor_agent.md) — pick tools ITERATIVELY (chained, LLM personas)
8. **Catalog Agent** *(this demo)* — pick from the LIVE REGISTRY, execute REAL, iteratively, with schema discovery

The progression: hand-authored → catalog-driven → real invocation → iterative → schema-aware. Each step handles a real limitation of the prior shape. Catalog Agent is the strongest shape — everything below it is a special case (set `max_iterations: 1` for single-shot, filter `include_ids` tight for a small tool set, etc.).

## Related

- [Iterative Supervisor Agent](./iterative_supervisor_agent.md) — same iterative shape but hand-authored LLM-persona tools instead of the live catalog.
- [MCP Tool Picker](./mcp_tool_picker.md) — MCP-backed real tools; single-shot.
