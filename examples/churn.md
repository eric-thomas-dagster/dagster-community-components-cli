# Churn prediction — synthetic customer aggregates → risk scores

A 3-component pipeline that generates 200 synthetic customer-level
aggregate rows (`last_activity`, `total_orders`, `total_revenue`,
`lifetime_days`), runs `churn_prediction` (rule-based: inactivity
threshold + risk factors), writes per-customer risk scores + tier +
recommended action.

## Pipeline

```
csv_file_ingestion → churn_prediction → dataframe_to_csv
```

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `csv_file_ingestion` | ingestion | Load 200 synthetic per-customer aggregates |
| 2 | `churn_prediction` | analytics | Score each customer (inactivity threshold = 60d, lookback = 365d) and assign a risk level + recommendation |
| 3 | `dataframe_to_csv` | sink | Write per-customer report |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_churn_demo.sh | bash
cd churn-demo
uv run dg launch --assets '*'
```

## Output

`/tmp/churn_predictions.csv` — every customer with:

- `days_inactive` — derived from `last_activity`
- `churn_risk_score` — 0–1 weighted score
- `churn_risk_level` — Low / Medium / High / Critical Risk
- `recommended_action` — text recommendation per tier
- `risk_factors` — comma-separated contributing factors
- `activity_trend` — derived signal

Risk distribution from a real run:

```
Critical Risk    72   (36%)   — 91+ days inactive, intervention urgent
Low Risk         52   (26%)
Medium Risk      52   (26%)
High Risk        24   (12%)
```

## What this demo shows

- **First demo using `churn_prediction`** — rule-based scoring (no ML
  model needed) that combines inactivity, order frequency, revenue,
  and lifetime into a weighted score. `include_risk_factors: true`
  attaches the contributing factors per row, so the output is
  interpretable not just numeric.
- **Customer-level aggregates as input.** The component expects one
  row per customer with the listed columns. To get there from
  transaction-level data, run `summarize` upstream (`group_by:
  customer_id`, `aggregations: {revenue: sum, txn_id: count, ...}`).
- **The synthetic-data heredoc pattern again.** For per-customer rollups
  with realistic distributions across "fresh / dormant / churned"
  cohorts, an inline Python heredoc is the right tool —
  `time_series_generator` is for time-series, not aggregates.

## Extending

Pair with `customer_segmentation` (RFM scoring) on the same input — the
two components answer different questions (who's churning soon vs. who
falls into Champion / At Risk / etc. RFM segments). Combine for a richer
CDP view.
