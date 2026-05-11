# Airline Passengers — fit ETS, forecast 24 months out

A 4-component pipeline that fits an Exponential Smoothing model (Holt-Winters)
to the classic 1949-1960 monthly airline passengers series and projects 24
months of forecast. The output CSV interleaves history and forecast.

## Pipeline

```
csv_file_ingestion → datetime_parser → ets_forecast → dataframe_to_csv
```

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `csv_file_ingestion` | ingestion | Pull the canonical Box-Jenkins airline-passengers CSV (144 rows, monthly) |
| 2 | `datetime_parser` | transformation | Parse `1949-01` strings into first-of-month datetimes |
| 3 | `ets_forecast` | transformation | Fit ETS with additive trend + multiplicative seasonality, forecast 24 periods |
| 4 | `dataframe_to_csv` | sink | Write 144 historical + 24 forecasted rows |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_passengers_forecast_demo.sh | bash
cd passengers-forecast-demo
uv run dg launch --assets '*'
```

## Output

`/tmp/passengers_forecast.csv` — 168 rows (144 history + 24 forecast):

```
Month,Passengers
1949-01-01,112.0          # historical
1949-02-01,118.0
...
1960-11-01,390.0          # last historical month
1960-12-01,432.0
1961-01-01,432.46         # forecast starts here
...
1962-12-01,488.52         # forecast ends 24 months later
```

The model captures the classic pattern: strong upward trend + 12-month
seasonal cycle (summer peaks, winter troughs).

## What this demo shows

- **Time-series forecasting from one `defs.yaml`.** No statsmodels boilerplate
  in user code — `ets_forecast` configures `trend`, `seasonal`,
  `seasonal_periods`, `forecast_periods`, and `output_mode` from YAML.
- **`output_mode: append`** — the result combines history and forecast into
  one continuous series, ready for plotting. Use `forecast` for just the
  projected window, or swap to `arima_forecast` for ARIMA / SARIMA models
  (same field shape).
- **Real model fit, no glue code.** The asset's metadata records column
  schema and lineage automatically; lineage chains back through the parser
  and ingestion.

## Extending

Swap `ets_forecast` for `arima_forecast` (set `order: [1, 1, 1]` and
optionally `seasonal_order: [1, 1, 1, 12]`) — same input shape, same output
modes, but ARIMA-style fit. Or change `forecast_periods` to project further.
