# NYC Weather — running totals + transpose + CSV

A 5-component pipeline that pulls 14 days of NYC weather from Open-Meteo,
parses dates, computes running precipitation, transposes into a metric-per-day
matrix, writes a CSV.

## Pipeline

```
rest_api_fetcher → datetime_parser → running_total → transpose → dataframe_to_csv
```

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `rest_api_fetcher` | ingestion | GET Open-Meteo with `json_path: daily` to extract the columnar `{time, temp_max, temp_min, precip_sum}` block |
| 2 | `datetime_parser` | transformation | Parse `time` strings (`YYYY-MM-DD`) into datetimes |
| 3 | `running_total` | transformation | Cumulative sum of `precipitation_sum` → `cumulative_precip_mm` |
| 4 | `transpose` | transformation | Pivot so dates are columns, metrics are rows |
| 5 | `dataframe_to_csv` | sink | Write `/tmp/weather_report.csv` |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_weather_demo.sh | bash
cd weather-demo
uv run dg launch --assets '*'
```

## Output

```
metric                2026-04-17  2026-04-18  2026-04-19  ...  2026-05-01
temperature_2m_max    25.9        21.6        11.4        ...  16.7
temperature_2m_min    19.9        11.5        5.8         ...  4.7
precipitation_sum     11.5        0.0         1.5         ...  0.0
cumulative_precip_mm  11.5        11.5        13.0        ...  37.1
```

## What this demo shows

- `rest_api_fetcher` handles **columnar dict responses** — when an API
  returns `{col: [v1, v2, ...]}` (parallel lists), the fetcher detects this
  and produces a normal row-oriented DataFrame instead of a 1-row df with
  list-valued cells.
- `running_total` supports `sum`/`min`/`max`/`mean`/`count` aggregations
  with optional grouping and sort.
- `transpose` flips rows ↔ columns, preserving an explicit index column —
  great for executive-summary tables.
