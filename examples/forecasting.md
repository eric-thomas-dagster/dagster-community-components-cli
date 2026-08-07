# Forecasting — ARIMA + ETS + train/val/test sampling
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

**Validated end-to-end** — RUN_SUCCESS in seconds. Three time-series
components on synthetic data with realistic seasonality + trend.

```
monthly_revenue (36 months, trend + 12-month seasonality)
       │
       └── monthly_revenue_forecast   ← arima_forecast (12 periods ahead)

weekly_orders (52 weeks, trend + ~quarterly cycle)
       │
       └── weekly_orders_forecast     ← ets_forecast (8 periods ahead)

churn_dataset (100 customers, 30% churn)
       │
       └── churn_dataset_split        ← create_samples (stratified train/val/test)
```

## Components used

| Component | What it produces |
|---|---|
| `arima_forecast` | One row per forecast period: `{date, forecast, lower_ci, upper_ci}`. Configurable `(p, d, q)` order + optional seasonal `(P, D, Q, m)`. |
| `ets_forecast` | Same shape; uses Holt-Winters exponential smoothing. `trend` and `seasonal` are additive or multiplicative. |
| `create_samples` | The original DataFrame plus a `split` column tagging each row `train` \| `val` \| `test`. Optional `stratify_column` preserves class balance. Alternative to `train_test_splitter` when you want a single tagged frame. |

## Cost

**$0.** statsmodels + pandas, all local.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_forecasting_demo.sh | bash
cd forecasting-demo
uv run dg launch --assets '*'
uv run dg dev   # http://localhost:3000
```

## When to pick which forecast

- **`arima_forecast`** for stationary or seasonally-differentiable
  series. Strong on macroeconomic-style data.
- **`ets_forecast`** for clearly trended + seasonal data with no need
  for `(p, d, q)` tuning. More robust default choice.
- For both: `forecast_periods` is the horizon. `confidence_level` (0–1)
  controls the prediction interval width (default 0.95).

## See also

Browse the [walkthrough index](README.md) for related demos across every component family.
