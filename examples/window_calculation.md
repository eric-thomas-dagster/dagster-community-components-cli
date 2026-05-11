# Window Calculation demo

Synthetic 3-symbol × 10-day stock-price ticks → one [`window_calculation`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/transforms/window_calculation)
component computes **every supported window function** in one pass.

```
csv → window_calculation → CSV
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | [`csv_file_ingestion`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/csv_file_ingestion) | ingestion | 30 rows of synthetic close prices |
| 2 | [`window_calculation`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/transforms/window_calculation) | transformation | row_number, rank, dense_rank, lag, lead, cumsum, moving_avg(3), moving_sum(5) |
| 3 | [`dataframe_to_csv`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_csv) | sink | Write the augmented DataFrame |

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
