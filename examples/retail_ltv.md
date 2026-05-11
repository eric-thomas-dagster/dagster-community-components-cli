# UCI Online Retail — customer lifetime value prediction

A 5-component customer-data-platform pipeline. Pulls the canonical UCI
Online Retail dataset (542k transactions from a UK e-commerce site,
2010-2011), drops rows with missing CustomerIDs, computes per-line
revenue, predicts each customer's 12-month LTV, segments them
Bronze/Silver/Gold/Platinum.

## Pipeline

```
csv_file_ingestion → data_cleansing → formula → ltv_prediction → dataframe_to_csv
```

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `csv_file_ingestion` | ingestion | Pull the UCI dataset (Databricks-mirrored CSV, ~45MB, 542k rows) |
| 2 | `data_cleansing` | transformation | Drop rows where `CustomerID` is null (~25% of raw — anonymous walk-ins) |
| 3 | `formula` | transformation | Compute `amount = Quantity * UnitPrice` per line |
| 4 | `ltv_prediction` | analytics | Predict 12-month LTV per customer; bucket into value segments |
| 5 | `dataframe_to_csv` | sink | Write the per-customer report |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_retail_ltv_demo.sh | bash
cd retail-ltv-demo
uv run dg launch --assets '*'
```

## Output

`/tmp/customer_ltv.csv` — one row per qualifying customer (those with ≥2
transactions). Columns include `customer_id`, `total_transactions`,
`avg_order_value`, `historical_ltv`, `predicted_total_ltv`,
`value_segment`, `ltv_percentile`.

Segment breakdown from a real run:

```
Bronze   (Bottom 50%):  2133 customers   ($1,281    avg LTV)
Silver   (Top 50%):     1066 customers   ($8,817    avg LTV)
Gold     (Top 25%):      641 customers   ($28,058   avg LTV)
Platinum (Top 10%):      427 customers   ($95,772   avg LTV)
```

Top 5 highest-LTV customers (the long tail is real):

```
customer_id  total_transactions  historical_ltv  predicted_total_ltv  value_segment
15098.0      3                   39,916.50       3,951,733.50         Platinum (Top 10%)
16000.0      9                   12,393.70       1,226,976.30         Platinum (Top 10%)
12590.0      68                  9,864.26        976,561.74           Platinum (Top 10%)
18139.0      159                 8,438.34        835,395.66           Platinum (Top 10%)
12357.0      131                 6,207.67        614,559.33           Platinum (Top 10%)
```

## What this demo shows

- **A real CDP pipeline composed entirely from registry components.**
  Ingest, clean, derive, model, sink — five YAML files, no Python glue.
- **`ltv_prediction` auto-detects column names**, but you can override
  them via `customer_id_field`, `transaction_date_field`, `amount_field`.
  Useful when the canonical names don't match (e.g. UCI's `CustomerID`
  vs. the auto-detect's `customer_id`).
- **Customer segmentation comes for free** — the asset's output
  includes a `value_segment` column (Bronze/Silver/Gold/Platinum) and an
  `ltv_percentile` column. Drop a `filter` downstream to materialize a
  "Platinum-only" segment for targeted campaigns.

## Extending to a fuller CDP

Once you have a customer-keyed dataframe, more registry components plug in:

- `rfm_segmentation` — Recency / Frequency / Monetary scoring (uses
  `source_asset` lineage, not `upstream_asset_key`)
- `churn_prediction` — predict which customers stop buying
- `customer_journey_mapping` — sequence analysis from event data
- `multi_touch_attribution` — distribute conversion credit across channels

All accept transaction-shaped dataframes; many auto-detect the same column
patterns (`customer_id`, `date`, `amount`).
