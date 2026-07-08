# Planned Catalog Agent — state-backed catalog_agent (LLM plans once, real assets forever)

**Component (new):** `PlannedCatalogAgentComponent` — the `dg.StateBackedComponent` variant of [`CatalogAgentComponent`](./catalog_agent.md). Runs the LLM planner + real materializations ONCE at prepare time (`write_state_to_path`) and caches the full plan to Dagster's native state store. Every subsequent load reads the cache and emits REAL Dagster assets — zero LLM cost per run.

**Script:** [`setup_planned_catalog_agent_demo.sh`](./setup_planned_catalog_agent_demo.sh)
**Cost:** ~$0.02 for the ONE trajectory. Every subsequent run is free.
**Validated:** 2026-07-07 — full pipeline planned once, real assets materialized end-to-end from cache with no LLM invocation.

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
3. REAL assets appear in the graph: `synthetic_orders_and_customers`, `joined_orders_and_customers`, `derived_month`, `summarized_orders`, `output_summarized_orders_csv`. Real names, real deps.
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

## Validated run output (2026-07-07)

**Task:** *"Generate synthetic orders and synthetic customers, join them, group by first name, email, and month, sum total and count of orders, and store to a csv."*

### Prepare step (`write_state_to_path`) — 62s, one LLM trajectory

```
iter 1: synthetic_data_generator → synthetic_orders_and_customers        ✓
iter 2: dataframe_join             → joined_orders_and_customers          ✓
iter 3: summarize                  → summarized_orders                    ✗ (no `month` yet)
iter 4: formula                    → derived_month                        ✓  (course-correct!)
iter 5: summarize                  → summarized_orders                    ✓
iter 6: dataframe_to_csv           → output_summarized_orders_csv         ✓
```

Plan cached to `src/<project>/defs/.local_defs_state/PlannedCatalogAgent__<hash>__/state`:

```json
{
  "task": "Generate synthetic orders and synthetic customers, ...",
  "task_hash": "d807f53f5bfe",
  "plan": [
    {"asset_name": "synthetic_orders_and_customers",
     "component_type": "dagster_community_components.SyntheticDataGeneratorComponent",
     "config": "{...}",  "status": "success"},
    {"asset_name": "joined_orders_and_customers",
     "component_type": "dagster_community_components.DataframeJoin",
     "config": "{...}",  "status": "success"},
    ...
  ]
}
```

### Load step (`build_defs_from_state`) — 2.85s, ZERO LLM calls

`dg list defs` after cache is populated:

```
default │ derived_month                  │ formula                  │ python │ ...
default │ joined_orders_and_customers    │ dataframe_join           │ python │ ...
default │ output_summarized_orders_csv   │ dataframe_to_csv         │ python │ ...
default │ summarized_orders              │ summarize                │ python │ ...
default │ synthetic_orders_and_customers │ synthetic_data_generator │ python │ ...
```

Materialize succeeds end-to-end (~7s) with no LLM calls. Ship it.

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
