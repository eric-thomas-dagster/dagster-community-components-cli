# Time-series advanced — comparison + covariates + per-group factory
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

**Validated end-to-end** — RUN_SUCCESS in seconds. 4 advanced ts_*
components run on synthetic data. All statsmodels, no SaaS.

```
monthly_sales (36 months, trend + seasonality)
       │
       ├── monthly_sales_forecast    ← ts_forecast (auto-selects ARIMA/ETS)
       └── arima_vs_ets_comparison   ← ts_compare (hold-out test, picks winner)

monthly_sales_with_covariates (+marketing_spend, +holiday_flag)
       │
       └── covariate_forecast        ← ts_covariate_forecast (SARIMAX with exog vars)

product_sales (5 products × 36 months)
       │
       └── per_product_forecasts     ← ts_model_factory (one model per product_id)
```

## Components used

| Component | Use case |
|---|---|
| `ts_forecast` | Single-series forecast. `model: auto` picks ARIMA or ETS based on AIC. |
| `ts_compare` | Train multiple models on a hold-out test set, report MAE/RMSE/MAPE per model. |
| `ts_covariate_forecast` | SARIMAX with exogenous regressors — useful when you have leading indicators (marketing spend, weather, holidays). |
| `ts_model_factory` | One forecast per group key. Per-product, per-region, per-tenant — without writing the loop yourself. |

## Cost

**$0.** statsmodels + pandas, all local.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_time_series_advanced_demo.sh | bash
cd time-series-advanced-demo
uv run dg launch --assets '*'
uv run dg dev   # http://localhost:3000
```

## When to pick which

- **`ts_forecast`** — you have one series, you don't want to think about
  models. Auto-selection is good enough for 80% of dashboards.
- **`ts_compare`** — you care about which model wins. Useful for a
  one-time analysis or a quarterly model selection.
- **`ts_covariate_forecast`** — you have leading indicators. Marketing
  spend forecasts revenue better than naive ARIMA.
- **`ts_model_factory`** — you have many series of the same shape
  (per-tenant, per-product, per-region). Each gets its own model
  fitted to its own history.

## Combine with PerPartitionBackfillJob

The `ts_model_factory` shape pairs naturally with the
`per_partition_backfill_job` (see `partitions.md`): make the asset
partitioned by `group_column`, and use the backfill job to
materialize one model-fit per partition with per-tenant concurrency.

## See also

<!-- TODO: link related walkthroughs -->
