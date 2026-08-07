# Window Calculation demo
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

Synthetic 3-symbol × 10-day stock-price ticks → one `window_calculation`
component computes **every supported window function** in one pass.

```
csv → window_calculation → CSV
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `file_ingestion` | ingestion | 30 rows of synthetic close prices |
| 2 | `window_calculation` | transformation | row_number, rank, dense_rank, lag, lead, cumsum, moving_avg(3), moving_sum(5) |
| 3 | `dataframe_to_csv` | sink | Write the augmented DataFrame |

## Window functions covered

```yaml
operations:
  - {output: row_num,       func: row_number}                                        # 1, 2, 3, ...
  - {output: close_rank,    func: rank,        column: close}                         # rank within partition
  - {output: close_drank,   func: dense_rank,  column: close}                         # dense rank
  - {output: prev_close,    func: lag,         column: close, periods: 1}             # value from prior row
  - {output: next_close,    func: lead,        column: close, periods: 1}             # value from next row
  - {output: cum_close,     func: cumsum,      column: close}                         # running total
  - {output: rolling_3_avg, func: moving_avg,  column: close, window: 3}              # rolling mean
  - {output: rolling_5_sum, func: moving_sum,  column: close, window: 5}              # rolling sum
```

`partition_by: [symbol]` + `order_by: [trade_date]` — operations are scoped to each symbol's chronological series.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_window_calculation_demo.sh | bash
cd window-calc-demo && uv run dg launch --assets '*'
```

## Expected output

30 rows × 11 columns. Per-symbol invariants the demo guarantees:
- `row_num` goes 1..10
- `prev_close` is null on day 1, then equals the previous row's `close`
- `cum_close` is monotonically non-decreasing
- `rolling_3_avg` smooths the noise; first 2 days use available data only

This is the sanity test for the window-function dispatcher — if any function
silently breaks, the per-row math diverges from expectation immediately.

---

## Also: build this from natural language

The [`planned_catalog_agent`](./planned_catalog_agent.md) component can plan and cache this pipeline from a natural-language task — no defs.yaml per component needed. Drop the following into a single defs.yaml, run `dg utils refresh-defs-state` once, and the real component assets appear in your graph.

```yaml
type: dagster_community_components.PlannedCatalogAgentComponent
attributes:
  task: |
    Take 400 synthetic order rows (schema_type: orders). Sort them by order_date
    ascending. Then compute several window calculations over the total column:
      - a 7-row rolling sum called rolling_sum_7
      - a 1-row lag called lag_1
      - a cumulative sum called cumulative_total
    Preserve all original columns plus the new window columns.
    Write the result to /tmp/order_windows.csv.
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

Live-validated on gpt-4o-mini: **4/5 clean picks in 10s, ~$0.0042 total cost.** Outputs written: `/tmp/order_windows.csv`.


After the trajectory runs once, materialization is pure cached-plan execution — no LLM per run.

## See also

Browse the [walkthrough index](README.md) for related demos across every component family.
