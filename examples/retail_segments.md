# UCI Online Retail — RFM customer segmentation

A 6-component pipeline that takes the same UCI Online Retail dataset as
the LTV demo and routes it through `customer_segmentation` to compute
Recency / Frequency / Monetary scores per customer and assign named
segments (Champions, Loyal Customers, At Risk, Lost, etc.).

## Pipeline

```
csv_file_ingestion → data_cleansing → formula → select_columns
                   → customer_segmentation → dataframe_to_csv
```

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `csv_file_ingestion` | ingestion | Pull UCI Online Retail (Databricks-mirrored CSV) |
| 2 | `data_cleansing` | transformation | Drop rows where `CustomerID` is null |
| 3 | `formula` | transformation | Compute `amount = Quantity * UnitPrice` |
| 4 | `select_columns` | transformation | Rename UCI's `CustomerID` → `customer_id`, `InvoiceDate` → `date`; keep `amount` |
| 5 | `customer_segmentation` | analytics | RFM scoring + named segments |
| 6 | `dataframe_to_csv` | sink | Write per-customer segments |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_retail_segments_demo.sh | bash
cd retail-segments-demo
uv run dg launch --assets '*'
```

## Output

`/tmp/customer_segments.csv` — 4,372 customers, RFM scores + named
segment + recommendation. Distribution from a real run:

```
Champions             946   Reward; early adopters; brand advocates
Hibernating           635   Offer related products; revive interest
Lost                  509   Recovery campaign or write off
Loyal Customers       472   Cross-sell; upgrade to higher tier
Promising             451   Free trials, content, attention
Potential Loyalists   362   Loyalty program candidates
About to Sleep        360   Reconnect with valuable resources
Need Attention        321   Limited time offers
At Risk               305   Personalized re-engagement
Cant Lose Them         11   Win back with what they value most
```

## What this demo shows

- **Lineage-based component wiring.** `customer_segmentation` uses
  `transaction_data_asset` (an asset name string), not
  `upstream_asset_key`. The component instantiates an `AssetIn` from
  that name internally — different shape than the transform components.
- **Schema adaptation via `select_columns` rename.** The UCI dataset
  uses `CustomerID` / `InvoiceDate`; `customer_segmentation` expects
  `customer_id` / `date`. One YAML field bridges the two without custom
  code.
- **`analysis_period_days: 6000`** — the UCI dataset is from 2010-2011,
  so the default 365-day window would filter everything out. Tune to
  match your data's vintage.

## Extending

Drop a `filter` step after `customer_segments` to materialize a
"Champions only" or "At Risk" cohort for targeted campaigns. Or wire the
segmented output into `revenue_attribution` to credit campaigns by segment.
