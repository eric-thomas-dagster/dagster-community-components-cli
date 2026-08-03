# Pivot ↔ Unpivot demo

Round-trip on monthly sales data: long → wide via `pivot`, then back to long via
`unpivot`. Verifies both transforms compose cleanly without data loss.

Different from `transpose`:
- `transpose` flips axes — every row becomes a column, every column becomes a row.
- `pivot` uses one column's values as new column headers + aggregates.
- `unpivot` (`melt`) is the inverse of `pivot`.

```
csv (long) → pivot (wide) → unpivot (long-again) → CSV
                          ↘ wide CSV
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `file_ingestion` | ingestion | 9 rows: month × region × revenue |
| 2 | `pivot` | transformation | Long → wide (regions become column headers) |
| 3 | `unpivot` | transformation | Wide → long (back to original shape) |
| 4 | `dataframe_to_csv` | sink | Wide report |
| 5 | `dataframe_to_csv` | sink | Long-again report (round-trip verification) |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_pivot_unpivot_demo.sh | bash
cd pivot-unpivot-demo && uv run dg launch --assets '*'
```

## Expected output

- `/tmp/pivot_demo/sales_wide.csv` — 3 rows (months) × 4 cols (`month, East, North, West`)
- `/tmp/pivot_demo/sales_long_again.csv` — 9 rows × 3 cols (`month, region, revenue`) — same shape as the source

---

## Also: build this from natural language

The [`planned_catalog_agent`](./planned_catalog_agent.md) component can plan and cache this pipeline from a natural-language task — no defs.yaml per component needed. Drop the following into a single defs.yaml, run `dg utils refresh-defs-state` once, and the real component assets appear in your graph.

```yaml
type: dagster_community_components.PlannedCatalogAgentComponent
attributes:
  task: |
    Build a small pipeline that:
      1. Generates 800 synthetic order rows (schema_type: orders).
      2. Derives a `month` column from order_date.
      3. Aggregates by category and month, summing total.
      4. Pivots to wide format — one row per category, one column per month, values are the sum of total.
      5. Also emits the same aggregated table in long form (category, month, total).
      6. Writes the WIDE table to /tmp/orders_wide.csv and the LONG table to /tmp/orders_long.csv.
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

Live-validated on gpt-4o-mini: **6/6 clean picks in 13s, ~$0.0045 total cost.** Outputs written: `/tmp/orders_wide.csv`, `/tmp/orders_long.csv`.


After the trajectory runs once, materialization is pure cached-plan execution — no LLM per run.

## See also

<!-- TODO: link related walkthroughs -->
