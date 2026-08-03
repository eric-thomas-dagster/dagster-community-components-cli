# Sales Trends (natural-language build)

Monthly revenue with a 3-month rolling average and month-over-month percent change — built from a natural-language task via [`planned_catalog_agent`](./planned_catalog_agent.md), no per-component defs.yaml.

## Pipeline

```
synthetic_data_generator (orders, 1500 — spans 365 days)
       │
       └─→ formula (add month column from order_date)
              │
              └─→ summarize (by month: revenue, orders_count)
                     │
                     └─→ sort → window_calculation (rolling_avg 3-mo)
                                    │
                                    └─→ pct_change → dataframe_to_csv
```

## The task

```yaml
type: dagster_community_components.PlannedCatalogAgentComponent
attributes:
  task: |
    Generate 1500 synthetic orders (schema_type: orders — order_date spans 365 days,
    has total column). Then:
      1. Derive a `month` column via formula: month = order_date.dt.to_period('M').astype(str).
      2. Aggregate by month using summarize:
           revenue = sum of total
           orders_count = count (row count per month)
      3. Sort by month ascending.
      4. Add a 3-month rolling average of revenue via window_calculation
         (func=moving_avg, column=revenue, window=3, order_by=month, output=rolling_avg_revenue).
      5. Add a month-over-month percent change of revenue via pct_change
         (value_column=revenue, output=revenue_pct_change).
      6. Write the final table to /tmp/sales_trends.csv.
  include_ids: [synthetic_data_generator]
  llm_model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  prefilter_llm: true
  prefilter_max_entries: 40
  max_iterations: 12
  defs_state: { management_type: LOCAL_FILESYSTEM, refresh_if_dev: false }
```

## Validated run (2026-07-08)

- **7/7 clean picks in 24.4s, ~$0.006 total cost** on gpt-4o-mini.
- 13 months of real revenue + rolling avg + MoM % written to `/tmp/sales_trends.csv`.

Sample output:

```
month,revenue,orders_count,rolling_avg_revenue,revenue_diff,revenue_pct
2025-07,58291.46,100,58291.46,,
2025-08,68068.02,127,63179.74,9776.56,0.1677
2025-09,77889.21,138,68082.90,9821.19,0.1443
2025-10,68163.77,132,71373.67,-9725.44,-0.1249
2025-11,76773.80,136,74275.59,8610.03,0.1263
```

## See also

- [Window Calculation](window_calculation.md) — deeper coverage of the window function set
- [Cohort Analysis](cohort_analysis.md) — the retention-over-time counterpart to trend analysis
- [Data Combination](data_combination.md) — same join+aggregate shape on customers instead of trends
