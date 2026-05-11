# Retail Customer Analytics demo

Generates 2000 synthetic orders, parses the dates, then runs TWO
parallel analytics branches off the typed dataset: RFM segmentation
(recency/frequency/monetary scoring) and monthly cohort retention.
Each branch sinks to its own CSV, plus a running-total view of
cumulative spend per customer.

Pipeline (7 components, all autoloaded by `dg`):
                             ┌─→ rfm_segmentation     → segments_csv
                             │
    synthetic_orders          ├─→ cohort_analysis      → cohorts_csv
      └─→ datetime_parser ───┤
                             └─→ running_total         → spend_csv

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `synthetic_data_generator` | ai | Generate synthetic data |
| 2 | `datetime_parser` | transformation | Parse date columns |
| 3 | `rfm_segmentation` | analytics | RFM customer segments |
| 4 | `cohort_analysis` | analytics | Cohort retention matrix |
| 5 | `running_total` | transformation | Cumulative aggregate |
| 6 | `dataframe_to_csv` | sink | Write CSV |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_retail_analytics_demo.sh | bash
cd retail-analytics-demo
uv run dg launch --assets '*'
```
