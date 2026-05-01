# SpaceX Launches — datetime parsing + ranking + Excel

A 5-component pipeline that pulls every SpaceX launch ever, parses the UTC
launch dates, ranks launches newest-first, writes an Excel report.

## Pipeline

```
rest_api_fetcher → select_columns → datetime_parser → rank → dataframe_to_excel
```

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `rest_api_fetcher` | ingestion | GET `api.spacexdata.com/v4/launches` (no auth) |
| 2 | `select_columns` | transformation | Keep `name`, `date_utc`, `success`, `rocket`, `flight_number`, `details` |
| 3 | `datetime_parser` | transformation | Parse `date_utc` → tz-aware datetime in `launch_date` |
| 4 | `rank` | transformation | Rank by `launch_date` descending → `rank_by_date` |
| 5 | `dataframe_to_excel` | sink | Write `/tmp/spacex_launches.xlsx` |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_spacex_demo.sh | bash
cd spacex-demo
uv run dagster asset materialize --select '*' -m definitions
```

## Output

`/tmp/spacex_launches.xlsx` — 205 rows, ranked newest-first. Sample (rank=1
is most recent):

```
name                  launch_date  rank_by_date
SWOT                  2022-12-05            1
O3b mPower 3.4        2022-12-01            2
Transporter-6         2022-12-01            2
```

## What this demo shows

- `datetime_parser` auto-detects ISO 8601 with millisecond `.000Z` precision
  — no `input_format` required.
- `rank` supports `dense` / `min` / `max` / `first` methods, ascending or
  descending, with a configurable output column.
- `dataframe_to_excel` automatically strips tz from tz-aware datetimes
  before writing (Excel doesn't support tz-aware) and logs which columns
  were touched.
