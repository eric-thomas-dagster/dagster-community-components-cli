# Forecast Comparison demo

evaluate which fits better via ts_compare.

Pipeline (8 components, all autoloaded by `dg`):
                                                   ┌─→ arima_forecast → CSV
  time_series_generator → ts_filler ──────────────┤
                                                   ├─→ ets_forecast   → CSV
                                                   │
                                                   └─→ ts_compare     → CSV (winner verdict)

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | [`time_series_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/time_series_generator) | analytics | Generate a synthetic time series |
| 2 | [`ts_filler`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/ts_filler) | transformation | Fill time-series gaps |
| 3 | [`arima_forecast`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/arima_forecast) | transformation | ARIMA forecast |
| 4 | [`ets_forecast`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/ets_forecast) | transformation | ETS forecast |
| 5 | [`ts_compare`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/ts_compare) | analytics | ARIMA vs ETS comparison |
| 6 | [`dataframe_to_csv`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_csv) | sink | Write CSV |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_forecast_comparison_demo.sh | bash
cd forecast-comparison-demo
uv run dg launch --assets '*'
```
