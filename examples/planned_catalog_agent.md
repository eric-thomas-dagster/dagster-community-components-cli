# Planned Catalog Agent — state-backed catalog_agent (LLM plans once, real assets forever)
> ✅ **Dagster+ Serverless:** deploys as-is via `dagster-cloud serverless deploy-docker`.

**Component (new):** `PlannedCatalogAgentComponent` — the `dg.StateBackedComponent` variant of [`CatalogAgentComponent`](./catalog_agent.md). Runs the LLM planner + real materializations ONCE at prepare time (`write_state_to_path`) and caches the full plan to Dagster's native state store. Every subsequent load reads the cache and emits REAL Dagster assets — zero LLM cost per run.

**Script:** [`setup_planned_catalog_agent_demo.sh`](./setup_planned_catalog_agent_demo.sh)
**Cost:** ~$0.02 for the ONE trajectory. Every subsequent run is free.
**Validated:** 2026-07-08 — 6-step orders/customers pipeline (~10s cached materialize, 751 rows) AND a 12-step Titanic ML pipeline (ingest CSV → dedup → cleanse → outlier-clip → impute → coerce → bin → one-hot → logreg → branched summarize + filter → 3 CSV sinks) — 14/14 clean picks on gpt-4o, 988 predictions + 439 survivors + 3 EDA rows on disk. See [Titanic case study](#titanic-case-study-larger-natural-language-pipeline) below.

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

## Titanic case study — larger natural-language pipeline

Same component, task 4× larger. This is the [Titanic complete demo](./titanic_complete.md) pipeline (12 components + 3 branched outputs) built entirely from natural language.

```yaml
type: dagster_community_components.PlannedCatalogAgentComponent
attributes:
  task: |
    Build a data-science pipeline on the Titanic passenger dataset at
    https://raw.githubusercontent.com/mostly-ai/public-demo-data/dev/titanic/titanic-with-labels.csv
    which has 1309 rows with columns: Port, Gender, Age, Ticket, Fare,
    Siblings/Spouses, Parents/Children, Survived (Yes/No).

    Do all of the following, in order:
      1. Ingest that CSV from the URL.
      2. Drop duplicate rows.
      3. Cleanse text columns (trim + lowercase Port, Gender, Ticket).
      4. Clip outliers on Age and Fare using IQR.
      5. Impute missing Age and Fare with median.
      6. Coerce Age and Fare to float.
      7. Bin Age into 4 tiles named child / young_adult / adult / senior.
      8. One-hot encode Port and Gender (drop_first=true).
      9. Fit a logistic regression predicting Survived from encoded features.
     10. Also produce a summary of mean Age, mean Fare, count grouped by Port.
     11. Also produce a survivors-only subset (Survived == 'Yes').
     12. Write THREE CSVs to disk (predictions / eda / survivors).
  include_categories: [ingestion, transformation, analytics, sink]
  llm_model: gpt-4o          # gpt-4o-mini won't handle the branching decision
  api_key_env_var: OPENAI_API_KEY
  max_iterations: 20
  defs_state: { management_type: LOCAL_FILESYSTEM, refresh_if_dev: false }
```

### Trajectory (14/14 clean, one-shot)

```
 1. file_ingestion            → titanic_data_ingestion
 2. unique_dedup               → titanic_data_no_duplicates
 3. data_cleansing             → titanic_data_cleansed
 4. outlier_clipper            → titanic_data_no_outliers
 5. imputation                 → titanic_data_imputed
 6. type_coercer               → titanic_data_coerced       ← branch point
 7. tile_binning               → titanic_data_with_age_band
 8. one_hot_encoding           → titanic_data_encoded
 9. logistic_regression_model  → titanic_predictions        (up=encoded)
10. summarize                  → titanic_eda                (up=coerced ← BRANCHED)
11. filter                     → titanic_survivors          (up=coerced ← BRANCHED)
12. dataframe_to_csv           → predictions_csv → /tmp/titanic_predictions.csv
13. dataframe_to_csv           → eda_csv         → /tmp/titanic_eda.csv
14. dataframe_to_csv           → survivors_csv   → /tmp/titanic_survivors.csv
```

Prepare took ~14 min on gpt-4o (LLM planning + 14 real in-process materializations). Cache-hit materialize ran the whole 12-asset DAG in **18 seconds** — no LLM.

Real output:
```
$ head -3 /tmp/titanic_eda.csv
Port,mean_age,mean_fare,count
cherbourg,32.05,45.40,238
queenstown,29.21,16.35,63

$ wc -l /tmp/titanic_*.csv
       4 /tmp/titanic_eda.csv         (3 groups)
     989 /tmp/titanic_predictions.csv (988 test-set predictions + probabilities)
     440 /tmp/titanic_survivors.csv   (439 real survivors after cleansing)
```

### What made it work

The 12-step task hit every hard case at once:
- **Branching:** three parallel outputs (predictions, EDA, survivors) from the same lineage — LLM must pick the right upstream for each (the branch point is `type_coercer`, BEFORE one-hot encoding drops the raw Port/Gender columns).
- **Column side-effects:** `one_hot_encoding` renames `Port` → `Port_cherbourg / Port_queenstown / Port_southampton`, so downstream `summarize(group_by=['Port'])` on the encoded upstream fails with `KeyError`.
- **Enum values:** `outlier_clipper.strategy` accepts `iqr|zscore|percentile` (not `clip`); `tile_binning.method` accepts `equal_width|equal_freq|custom` (not `quantile`).
- **Case sensitivity:** `data_cleansing` with `normalize_case: lower` lowercases values — downstream `filter("Survived == 'Yes'")` matches 0 rows unless the LLM either lists columns explicitly OR uses `'yes'`.
- **Sinks required:** LLM must not declare done before writing all three CSVs.
- **Model choice matters:** gpt-4o-mini couldn't reason through the branching decision (kept picking encoded data as summarize upstream despite the KeyError); gpt-4o got it right on the first try. Cost is ~$0.60 for the one-shot plan, then cached free forever.

The `agent_hints` structured metadata on each component drives most of this. Each of the 12 titanic components declares `inputs`, `outputs`, `side_effects`, `anti_uses`, and `chains_with` — the planner reads these instead of relying on prose descriptions alone. That's what lets a natural-language task like "one-hot encode Port and Gender, then summarize by Port" resolve correctly: the planner sees `one_hot_encoding.side_effects: "REMOVES the source columns and adds MANY new <col>_<value> columns"` and picks an earlier upstream for summarize.

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

## See also

- [`catalog_agent`](./catalog_agent.md) — the per-step exploration variant.
- [`iterative_supervisor_agent`](./iterative_supervisor_agent.md) — same iterative shape but LLM-persona tools instead of the live catalog.
