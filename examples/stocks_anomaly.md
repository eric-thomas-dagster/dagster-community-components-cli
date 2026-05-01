# Stocks anomaly detection

A 3-component pipeline that pulls 10 years of monthly close prices for 5
tickers, runs z-score anomaly detection grouped by symbol (so MSFT outliers
are evaluated against MSFT's history, not pooled with AMZN's wider range),
writes a flagged report.

## Pipeline

```
csv_file_ingestion → anomaly_detection → dataframe_to_csv
```

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `csv_file_ingestion` | ingestion | Pull vega's stocks.csv (560 rows: MSFT/AMZN/IBM/GOOG/AAPL × 112 months each) |
| 2 | `anomaly_detection` | analytics | z-score within each `symbol` group; flag points beyond 2.5σ |
| 3 | `dataframe_to_csv` | sink | Write the flagged report |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_stocks_anomaly_demo.sh | bash
cd stocks-anomaly-demo
uv run dg launch --assets '*'
```

## Output

`/tmp/stocks_anomalies.csv` — every monthly close + an `is_anomaly`
flag and `anomaly_score`. ~8 anomalies out of 560 records (1.4%) at
threshold=2.5. Top hits cluster around AAPL's 2007 spike and AMZN's
2009 inflection.

## What this demo shows

- **Per-group anomaly detection.** `group_by_field: symbol` keeps the
  z-score calculation scoped to each ticker so a $100 swing for AMZN
  doesn't drag MSFT's threshold up. Same pattern applies to per-customer
  / per-region anomaly hunting.
- **Detection methods are configurable.** `detection_method` accepts
  `z_score`, `iqr`, `moving_average`, or `threshold` — same component,
  different math.
- **Anomaly metadata.** The asset emits `anomaly_count` / `anomaly_rate`
  / `detection_method` / `threshold` to the Dagster catalog; the top
  5 anomalies are also logged at materialize time for quick triage.
