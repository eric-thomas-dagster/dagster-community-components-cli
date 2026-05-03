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
| 1 | `csv_file_ingestion` | ingestion | 9 rows: month × region × revenue |
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
