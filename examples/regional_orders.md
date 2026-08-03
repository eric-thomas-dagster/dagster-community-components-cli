# Multi-region orders union demo

Three regions (NA, EU, APAC) export their order extracts as separate
CSVs with slightly different column sets (NA uses USD, EU uses EUR,
APAC includes a tax column the others don't). dataframe_union stacks
them with `join: outer` so the missing columns become NaN, then
dataframe_to_csv writes the unified extract.

Pipeline (5 components, all autoloaded by `dg`):
    file_ingestion x 3 → dataframe_union → dataframe_to_csv

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `file_ingestion` | ingestion | Read source CSV |
| 2 | `dataframe_union` | transformation |  |
| 3 | `dataframe_to_csv` | sink | Write CSV |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_regional_orders_demo.sh | bash
cd regional-orders-demo
uv run dg launch --assets '*'
```

---

## Also: build this from natural language

The [`planned_catalog_agent`](./planned_catalog_agent.md) component can plan and cache this pipeline from a natural-language task — no defs.yaml per component needed. Drop the following into a single defs.yaml, run `dg utils refresh-defs-state` once, and the real component assets appear in your graph.

```yaml
type: dagster_community_components.PlannedCatalogAgentComponent
attributes:
  task: |
    Generate two independent batches of synthetic orders (schema_type: orders):
      - Batch A: 200 rows with random_state 1
      - Batch B: 200 rows with random_state 2
    Then union the two batches into a single DataFrame (row-wise stack, all
    columns preserved). Write the combined result to /tmp/regional_orders.csv.
  include_ids: ['synthetic_data_generator']
  llm_model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  prefilter_llm: true
  prefilter_max_entries: 40
  max_iterations: 20
  defs_state:
    management_type: LOCAL_FILESYSTEM
    refresh_if_dev: false
```

Live-validated on gpt-4o-mini: **4/4 clean picks in 7s, ~$0.0036 total cost.** Outputs written: `/tmp/regional_orders.csv`.


After the trajectory runs once, materialization is pure cached-plan execution — no LLM per run.

## See also

<!-- TODO: link related walkthroughs -->
