# Forecast Comparison demo

evaluate which fits better via ts_compare.

Pipeline (8 components, all autoloaded by `dg`):
                                                   ┌─→ arima_forecast → CSV
  time_series_generator → ts_filler ──────────────┤
                                                   ├─→ ets_forecast   → CSV
                                                   │
                                                   └─→ ts_compare     → CSV (winner verdict)

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_forecast_comparison_demo.sh | bash
cd forecast-comparison-demo
uv run dg launch --assets '*'
```
