# Router demo

Multi-output conditional split on 30 synthetic orders — [`router`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/transforms/router) emits one
asset per route, exclusive matching (each row goes to exactly one bucket).

Equivalent to ADF's Conditional Split / Informatica Router.

```
                  ┌─→ high_value_orders   → CSV
csv → router ─────┼─→ medium_value_orders → CSV
                  └─→ low_value_orders    → CSV  (default — catch-all)
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | [`csv_file_ingestion`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/csv_file_ingestion) | ingestion | 30 synthetic orders with mixed totals |
| 2 | [`router`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/transforms/router) | transformation | Split into high/medium/low by `total` |
| 3-5 | [`dataframe_to_csv`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_csv) × 3 | sink | One CSV per bucket |

## Routes

```yaml
routes:
  - asset_name: high_value_orders
    condition: "total > 1000"
  - asset_name: medium_value_orders
    condition: "total >= 100 and total <= 1000"
default_asset_name: low_value_orders
exclusive: true   # each row goes to exactly one bucket (first-match wins)
```

`exclusive: false` would allow overlap (a row matching multiple conditions appears in multiple outputs).

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_router_demo.sh | bash
cd router-demo && uv run dg launch --assets '*'
```

## Expected output

3 CSVs in `/tmp/router_demo/`. Sum of rows = input row count (no row lost or
duplicated under `exclusive: true`).
