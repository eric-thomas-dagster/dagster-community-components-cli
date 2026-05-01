# GitHub Releases — filter + sort + Parquet

A 6-component pipeline that pulls the last 50 releases of `dagster-io/dagster`
from GitHub, parses publish dates, filters to stable releases (no prereleases
or drafts), sorts newest-first, writes Parquet.

## Pipeline

```
rest_api_fetcher → select_columns → datetime_parser → filter → sort → dataframe_to_parquet
```

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `rest_api_fetcher` | ingestion | GET `api.github.com/repos/dagster-io/dagster/releases?per_page=50` (no auth, ~60 req/hr/IP) |
| 2 | `select_columns` | transformation | Keep `tag_name`, `name`, `published_at`, `prerelease`, `draft`, `html_url` |
| 3 | `datetime_parser` | transformation | Parse `published_at` ISO 8601 → tz-aware datetime in `published_dt` |
| 4 | `filter` | transformation | `prerelease == False and draft == False` |
| 5 | `sort` | transformation | By `published_dt` descending |
| 6 | `dataframe_to_parquet` | sink | Write `/tmp/dagster_releases.parquet` |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_releases_demo.sh | bash
cd releases-demo
uv run dagster asset materialize --select '*' -m definitions
```

## Output

```
tag_name             published_dt              html_url
1.13.3   2026-04-30 20:46:18+00:00  https://github.com/dagster-io/dagster/releases/tag/1.13.3
1.13.2   2026-04-23 18:30:38+00:00  https://github.com/dagster-io/dagster/releases/tag/1.13.2
1.13.1   2026-04-17 19:33:16+00:00  ...
```

## What this demo shows

- `filter` accepts arbitrary `df.query`-style boolean expressions — chain
  conditions with `and` / `or` / `not`.
- `sort` accepts a list of columns plus per-column ascending flags.
- Parquet preserves tz-aware datetimes natively (unlike Excel) — round-trips
  cleanly into pandas with the original timezone.
- Six components composed via `Definitions.merge` with no glue code.
