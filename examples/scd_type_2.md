# SCD Type 2 demo

Slowly Changing Dimension Type 2 (history-tracking) on a synthetic 4-customer
snapshot. Validates the merge-and-expire logic end-to-end.

```
csv (yesterday) ─┐
                 ├─→ scd_type_2 → CSV
csv (today)     ─┘
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `file_ingestion` | ingestion | Yesterday's snapshot (with effective_from) |
| 2 | `file_ingestion` | ingestion | Today's incoming snapshot |
| 3 | `scd_type_2` | transformation | Merge: expire+insert when changed, keep when missing |
| 4 | `dataframe_to_csv` | sink | Write the resulting SCD2 history |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_scd_type_2_demo.sh | bash
cd scd-type-2-demo
uv run dg launch --assets '*'
```

## Expected output

5 rows in `/tmp/scd_demo/customers_scd2_output.csv`:

| customer | rows | reason |
|---|---|---|
| C001 | 1 (current) | Unchanged |
| C002 | 2 (1 expired + 1 current) | Plan tier upgraded — effective_from preserved on expired row |
| C003 | 1 (current) | Missing from incoming — kept active |
| C004 | 1 (current) | Net-new |

## Why it exists

Validates two specific bugs the component had at first ship:
1. The `left_only` branch (rows in target but not incoming) had its dict-comprehension key/value swapped — output had `_old`-suffixed columns full of NaN.
2. The expired-version branch dropped `effective_from`, breaking the historical timeline.

Both fixed. The demo guarantees they stay fixed.

---

## Also: build this from natural language

The [`planned_catalog_agent`](./planned_catalog_agent.md) component can plan and cache this pipeline from a natural-language task — no defs.yaml per component needed. Drop the following into a single defs.yaml, run `dg utils refresh-defs-state` once, and the real component assets appear in your graph.

```yaml
type: dagster_community_components.PlannedCatalogAgentComponent
attributes:
  task: |
    Generate two versions of a customer dimension (schema_type: customers):
      - existing: 200 rows, random_state 1  (this is the current dimension)
      - incoming: 250 rows, random_state 1  (this is the new incoming batch)
    Use scd_type_2 with customer_id as the natural key to merge the incoming
    batch into the existing dimension — new customer_ids get inserted with
    is_current=true; existing customer_ids whose columns changed get closed out
    (effective_to filled) and a new row added.
    Write the resulting dimension history to /tmp/customers_history.csv.
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

Live-validated on gpt-4o-mini: **4/4 clean picks in 9s, ~$0.0037 total cost.** Outputs written: `/tmp/customers_history.csv`.


After the trajectory runs once, materialization is pure cached-plan execution — no LLM per run.

## See also

<!-- TODO: link related walkthroughs -->
