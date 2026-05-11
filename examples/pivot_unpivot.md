# Pivot ↔ Unpivot demo

Round-trip on monthly sales data: long → wide via [`pivot`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/transforms/pivot), then back to long via
[`unpivot`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/transforms/unpivot). Verifies both transforms compose cleanly without data loss.

Different from [`transpose`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/transpose):
- [`transpose`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/transpose) flips axes — every row becomes a column, every column becomes a row.
- [`pivot`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/transforms/pivot) uses one column's values as new column headers + aggregates.
- [`unpivot`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/transforms/unpivot) (`melt`) is the inverse of [`pivot`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/transforms/pivot).

```
csv (long) → pivot (wide) → unpivot (long-again) → CSV
                          ↘ wide CSV
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | [`csv_file_ingestion`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/csv_file_ingestion) | ingestion | 9 rows: month × region × revenue |
| 2 | [`pivot`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/transforms/pivot) | transformation | Long → wide (regions become column headers) |
| 3 | [`unpivot`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/transforms/unpivot) | transformation | Wide → long (back to original shape) |
| 4 | [`dataframe_to_csv`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_csv) | sink | Wide report |
| 5 | [`dataframe_to_csv`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_csv) | sink | Long-again report (round-trip verification) |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_pivot_unpivot_demo.sh | bash
cd pivot-unpivot-demo && uv run dg launch --assets '*'
```

## Expected output

- `/tmp/pivot_demo/sales_wide.csv` — 3 rows (months) × 4 cols (`month, East, North, West`)
- `/tmp/pivot_demo/sales_long_again.csv` — 9 rows × 3 cols (`month, region, revenue`) — same shape as the source
