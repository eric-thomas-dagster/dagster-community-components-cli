# Planned Catalog Agent — state-backed catalog_agent (LLM plans once, real assets forever)

**Component (new):** `PlannedCatalogAgentComponent` — the `dg.StateBackedComponent` variant of [`CatalogAgentComponent`](./catalog_agent.md). Runs the LLM planner + real materializations ONCE at prepare time (`write_state_to_path`) and caches the full plan to Dagster's native state store. Every subsequent load reads the cache and emits REAL Dagster assets — zero LLM cost per run.

**Script:** [`setup_planned_catalog_agent_demo.sh`](./setup_planned_catalog_agent_demo.sh)
**Cost:** ~$0.02 for the ONE trajectory. Every subsequent run is free.
**Validated:** 2026-07-08 — 6-step pipeline planned once, real assets materialized from cache in ~10s with **751 real rows written to disk** (not empty output).

## Why this exists

[Catalog Agent](./catalog_agent.md) is powerful for exploration — you can see the step-by-step trajectory, planner reasoning, and course-correction in the asset graph. But every run invokes the LLM again. That's fine for demos and dev iteration; it's not what you want in production.

The two "graduation" paths from catalog_agent are:

1. **`codegen_output_dir`** — write real defs.yaml files to disk, commit them, remove the agent config. Pipeline is now code you can review and version. Best when you want the plan under source control.
2. **`PlannedCatalogAgentComponent`** *(this demo)* — same LLM trajectory, but cached via Dagster's state store. No files to commit; the plan lives in `.local_defs_state/`. Best when you want the "input a task in the UI, real assets appear" UX.

## The Dagster+ UI story

1. In the Dagster+ UI, create a new `defs.yaml`:
   ```yaml
   type: dagster_community_components.PlannedCatalogAgentComponent
   attributes:
     task: |
       Generate synthetic orders and customers, join them,
       group by first_name, email, and month, sum total and count orders,
       and store to /tmp/orders_by_customer_month.csv.
     include_categories: [source, ingestion, transformation, sink]
     include_ids: [synthetic_data_generator]
   ```
2. Save. Code-server reload (or explicit `dg utils refresh-defs-state`) triggers `write_state_to_path` — LLM plans the pipeline once.
3. REAL assets appear in the graph: `synthetic_orders`, `synthetic_customers`, `joined_orders_customers`, `derived_month`, `summarized_orders`, `output_orders_summary`. Real names, real deps.
4. Materialize normally. Zero LLM cost on every subsequent run.
5. To re-plan: edit the task, save, refresh state → new plan.

## vs `catalog_agent`

| | catalog_agent | planned_catalog_agent |
|---|---|---|
| Base class | `dg.Component` | `dg.StateBackedComponent` |
| When LLM runs | Every materialization | Only at prepare time (state refresh) |
| DAG shape | `step_1 → step_2 → ... → synthesis` (wrapper assets) | REAL component assets — no wrappers |
| Best for | Exploration, transparent trajectory | Production, "input task → assets appear" |
| Re-plan trigger | Change YAML, materialize | Change YAML, refresh state |
| Cost after prepare | LLM every run | Zero LLM |

## Prerequisites

- `uv` + `OPENAI_API_KEY` + internet (fetches manifest.json)

## Run

```bash
export OPENAI_API_KEY=sk-...
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_planned_catalog_agent_demo.sh -o setup_planned_catalog_agent_demo.sh
chmod +x setup_planned_catalog_agent_demo.sh
./setup_planned_catalog_agent_demo.sh
```

## Validated run output (2026-07-08)

**Task (natural user phrasing, no wiring hints):** *"Generate synthetic orders and synthetic customers, join them, group by first name, email, and month, sum total and count of orders, and store to a csv."*

### Prepare step (`write_state_to_path`) — ~2 min, one LLM trajectory

```
iter 1: synthetic_data_generator (schema=orders)    → synthetic_orders          ✓
iter 2: synthetic_data_generator (schema=customers) → synthetic_customers       ✓
iter 3: dataframe_join                              → joined_orders_customers   ✓
        left_asset_key=synthetic_orders, right_asset_key=synthetic_customers
iter 4: formula                                     → derived_month             ✓
        expressions: {month: joined_orders_customers.order_date.dt.month}
iter 5: summarize                                   → summarized_orders         ✓
        group_by: [first_name, email, month], agg: {total: sum, count: count}
iter 6: dataframe_to_csv                            → output_orders_summary     ✓
        file_path: /tmp/planned_output.csv
```

Notice this is a clean one-shot plan — the planner correctly generates BOTH sources up front (iter 1 + 2) with different `schema_type` values, then joins them at iter 3 with DIFFERENT `left_asset_key` / `right_asset_key`. Two guards in the trajectory make this reliable:

1. **Planner prompt** — explicit rule that `synthetic_data_generator` is single-schema-per-call, and that `dataframe_join` requires two DIFFERENT upstream assets.
2. **Server-side self-join guard** — if the planner ever picks `left_asset_key == right_asset_key`, the step fails with a specific error that the next iteration's planner sees in `prior_summary`. The trajectory course-corrects.

Plan cached to `src/<project>/defs/.local_defs_state/PlannedCatalogAgent__<hash>__/state`:

```json
{
  "task": "Generate synthetic orders and synthetic customers, ...",
  "task_hash": "d807f53f5bfe",
  "plan": [
    {"asset_name": "synthetic_orders",  "component_type": ".SyntheticDataGeneratorComponent", "status": "success"},
    {"asset_name": "synthetic_customers","component_type": ".SyntheticDataGeneratorComponent","status": "success"},
    {"asset_name": "joined_orders_customers","component_type": ".DataframeJoin", "status": "success"},
    {"asset_name": "derived_month",     "component_type": ".FormulaComponent",   "status": "success"},
    {"asset_name": "summarized_orders", "component_type": ".SummarizeComponent", "status": "success"},
    {"asset_name": "output_orders_summary","component_type": ".DataframeToCsvComponent","status": "success"}
  ]
}
```

### Load step (`build_defs_from_state`) — seconds, ZERO LLM calls

`dg list defs` after cache is populated:

```
default │ derived_month             │ formula                  │ python │ ...
default │ joined_orders_customers   │ dataframe_join           │ python │ ...
default │ output_orders_summary     │ dataframe_to_csv         │ python │ ...
default │ summarized_orders         │ summarize                │ python │ ...
default │ synthetic_customers       │ synthetic_data_generator │ python │ ...
default │ synthetic_orders          │ synthetic_data_generator │ python │ ...
```

### End-to-end materialize — 10 seconds, 751 real rows written

```
$ uv run dagster asset materialize --select '*' -m planned_test.definitions
...
INFO  Wrote 751 rows to /tmp/planned_output.csv
RUN_SUCCESS  Finished execution of run for "__ASSET_JOB"
```

Real CSV content:
```
first_name,email,month,total,count
David,david.brown253@example.com,6,296.74,1
David,david.brown357@example.com,6,680.88,1
David,david.brown404@example.com,7,481.13,1
Emily,emily.jones82@example.com,6,442.85,1
Jane,jane.davis836@example.com,6,128.12,1
John,john.rodriguez857@example.com,7,639.17,1
...
```

The planner spent ~2 min on the trajectory ONCE. Every subsequent materialization runs the real component pipeline in ~10s with no LLM cost. Ship it.

## How `refresh_if_dev` controls when the LLM runs

`StateBackedComponent` refreshes state during `INITIALIZATION` when either:
- The management type is `LEGACY_CODE_SERVER_SNAPSHOTS`, OR
- `using_dagster_dev()` is true AND `refresh_if_dev: true` (default)

For deterministic "cache is source of truth" behavior in demos, set:

```yaml
defs_state:
  management_type: LOCAL_FILESYSTEM
  refresh_if_dev: false
```

Now the trajectory ONLY runs when you invoke `dg utils refresh-defs-state` explicitly. Perfect for CI, for Dagster+ code-server deployments, and for demos where you want to prove the cache-hit story.

## Refreshing state

```bash
# Automatic — `dagster dev` re-runs write_state_to_path when
# refresh_if_dev is true (the default).
dagster dev

# Explicit refresh — always works, regardless of refresh_if_dev
dg utils refresh-defs-state
```

State key includes a hash of the task string so different tasks get different state files. Change the task → different key → next refresh writes fresh state.

## Comparison to `codegen_output_dir` graduation

Both eliminate per-run LLM cost. Pick based on where the plan should live:

| | `codegen_output_dir` (files) | `PlannedCatalogAgentComponent` (state) |
|---|---|---|
| Plan lives in | `src/<project>/defs/<asset>/defs.yaml` (git) | `.local_defs_state/` (state store) |
| Diff-reviewable | ✓ yes | ✗ opaque JSON |
| Refactorable by hand | ✓ yes | ✗ managed by component |
| Best for | "One-time exploration, then ship the plan" | "Task changes occasionally, re-plan lives in state" |
| Works in Dagster+ UI-only flow | needs a code push | ✓ pure UI + refresh button |

If you want engineers to review the pipeline before it hits prod, use codegen. If you want the "product manager writes a task, real assets appear" flow, use `PlannedCatalogAgentComponent`.

## Related

- [`catalog_agent`](./catalog_agent.md) — the per-step exploration variant.
- [`iterative_supervisor_agent`](./iterative_supervisor_agent.md) — same iterative shape but LLM-persona tools instead of the live catalog.
