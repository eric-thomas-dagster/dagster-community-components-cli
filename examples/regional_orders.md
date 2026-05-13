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
