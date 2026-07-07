# Component Catalog Agent — the catalog IS the curated set

**Component (new):** `ComponentCatalogAgentComponent` — fetches the LIVE manifest at runtime, presents a filtered slice to a planner LLM, and executes the picks via reflection + `dg.materialize()` in-process. **Real invocation, not simulation.**

**Script:** [`setup_component_catalog_agent_demo.sh`](./setup_component_catalog_agent_demo.sh)
**Cost:** ~$0.02 per run (planner + synthesis on gpt-4o-mini; the actual component executions are free — real Dagster asset materializations)
**Validated:** 2026-07-07 — real 4-component chained pipeline. Planner picked `synthetic_data_generator` → `filter` → `summarize` → `dataframe_to_csv` in order, chained via `upstream_asset_key`, and the executor materialized the whole graph in-process. All 4 succeeded; a real CSV landed on disk with the actual aggregated data:
```
type,row_count,total_amount
deposit,6,4709.12
refund,6,4351.64
```

## Why this exists

The bounded-action-space idea from [Supervisor Agent](./supervisor_agent.md) and [MCP Tool Picker](./mcp_tool_picker.md) has a scaling problem: the tool list is hand-authored in YAML. When you have 900+ components in the registry, you can't hand-write each one as a tool spec.

**The insight: the manifest IS the curated set.** Filter it (by category, tags, or explicit IDs) and hand the filtered slice to the planner. That filter is the bounded action space. No hand-authoring.

Even better: since all component classes are already importable from `dagster_community_components` (that's the package the CLI installs), we can **execute picks for real** — no simulation, no subprocess trickery. Reflection-based instantiation via Pydantic, in-process materialization via `dg.materialize()`.

## Pipeline

```
catalog_plan       (planner: fetch manifest → filter → introspect Pydantic fields
                    with TYPES → pick ORDERED list of {id, config} with upstream_asset_key
                    chaining)
       ↓
catalog_execution  (Phase 1: import + instantiate every pick; collect ALL assets
                    across ALL picks into one graph. Phase 2: dg.materialize()
                    runs the whole graph, Dagster resolves cross-pick dependencies
                    via asset_key ↔ upstream_asset_key matching. Phase 3: report
                    per-pick status + real output preview.)
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

## Validated run output (2026-07-07) — 4-component chained pipeline

**Task:** *"Build a mini analytics pipeline: generate 100 transactions → filter to amount>500 → summarize by type → write CSV."*

**Planner's picks — 4 different components, ordered + chained via `upstream_asset_key`:**

```
1. synthetic_data_generator  {asset_name: synthetic_transactions,  schema_type: transactions, row_count: 100}
2. filter                    {asset_name: filtered_transactions,   upstream_asset_key: synthetic_transactions, condition: "amount > 500"}
3. summarize                 {asset_name: transaction_summary,     upstream_asset_key: filtered_transactions, group_by: ["type"], aggregations: {row_count: {col:amount,agg:count}, total_amount: {col:amount,agg:sum}}}
4. dataframe_to_csv          {asset_name: transaction_summary_csv, upstream_asset_key: transaction_summary,   file_path: "/tmp/catalog_agent_demo_summary.csv"}
```

**Real execution — Dagster materialized the whole graph in one pass:**

```
transaction_summary output (real DataFrame):
| type    |   row_count |   total_amount |
|---------|-------------|----------------|
| deposit |           6 |        4709.12 |
| refund  |           6 |        4351.64 |
```

**Real CSV written to disk:**

```
$ cat /tmp/catalog_agent_demo_summary.csv
type,row_count,total_amount
deposit,6,4709.12
refund,6,4351.639999999999
```

That's a real 4-step Dagster asset pipeline, discovered from the live 900-component manifest, chained via `upstream_asset_key`, materialized end-to-end. No hand-authored tool list — the manifest IS the tool set.

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

- **Static schema knowledge only.** The planner sees the Pydantic field TYPES (accurate — Pydantic is source of truth) but doesn't know the actual COLUMNS of DataFrames flowing between picks. For synthetic data with well-known schemas, we currently spell the columns out in the task string. For customer-built data (e.g. a Snowflake table you own), the natural v2 is an **iterative catalog agent** — planner picks step 1 → executor runs it → agent inspects the real output columns → planner replans step 2 with the actual schema.
- **Resource-requiring components fail gracefully.** Snowflake / S3 / Slack / etc. components can't run without their resources wired at runtime. `fail_on_execution_error: false` (default) logs the failure and lets sibling picks continue. Set `true` for strict mode.
- **Multi-asset components get one output captured.** Fine for source-style components (1 asset). For fan-out components like `supervisor_agent` (N assets), the executor captures the first output — imperfect but works for the demo.

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
