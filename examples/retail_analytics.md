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

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_retail_analytics_demo.sh | bash
cd retail-analytics-demo
uv run dg launch --assets '*'
```
