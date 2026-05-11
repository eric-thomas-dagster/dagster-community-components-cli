# SaaS metrics — synthetic Stripe → MRR / ARR / churn / LTV

A 3-component pipeline that generates a Stripe-shaped CSV (50 synthetic
subscriptions across active / trialing / canceled states), runs
`subscription_metrics`, writes a SaaS-dashboard CSV with MRR, ARR,
churn rate, LTV, and ARPU.

## Pipeline

```
csv_file_ingestion → subscription_metrics → dataframe_to_csv
```

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `csv_file_ingestion` | ingestion | Load the synthetic subscriptions CSV |
| 2 | `subscription_metrics` | analytics | Compute MRR / ARR / churn / LTV / ARPU from Stripe-shaped data |
| 3 | `dataframe_to_csv` | sink | One-row metrics snapshot |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_saas_metrics_demo.sh | bash
cd saas-metrics-demo
uv run dg launch --assets '*'
```

The setup script generates `/tmp/stripe_subscriptions.csv` via inline
Python before scaffolding — 50 synthetic rows with Stripe-shaped columns
(`id`, `_resource_type`, `customer_id`, `status`, `created`,
`canceled_at`, `current_period_end`, `plan_amount`, `plan_interval`,
`plan_nickname`).

## Output

`/tmp/saas_metrics.csv` — sample run:

```
metric_date,mrr,arr,active_subscriptions,new_subscriptions,churned_subscriptions,
churn_rate,new_mrr,expansion_mrr,contraction_mrr,net_mrr_growth_rate,arpu,ltv

2026-05-01,1740.0,20880.0,40,36,8,66.67,0.0,0.0,0.0,0.0,43.50,783.0
```

MRR $1,740 / ARR $20,880, 40 active + 8 churned in lookback. ARPU
$43.50, historical LTV $783.

## What this demo shows

- **First synthetic-data demo.** The setup script emits a Stripe-shaped
  CSV with a Python heredoc — useful for any pipeline where the dataset
  is hard to get clean publicly (Stripe data, internal metrics, etc.).
- **`subscription_metrics` consumes Stripe directly.** The component
  expects columns `status`, `created`, `canceled_at`,
  `current_period_end`, `plan_amount`, etc. — a real Stripe export from
  `stripe sigma` will work without rename. Synthetic data here matches
  that schema.
- **Lineage-based wiring** (`stripe_data_asset: stripe_data`) — same
  shape as customer_segmentation and rfm_segmentation. Multiple lineage
  inputs are supported via the optional `revenue_data_asset` and
  `customer_360_asset` fields.
