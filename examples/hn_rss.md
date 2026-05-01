# Hacker News RSS — XML feed parsing

A 5-component pipeline that fetches the HN front-page RSS feed (raw XML),
splits it into one row per `<item>`, extracts each item's title and link
with a regex capture-group pair, drops the empty preamble, writes a
clean CSV.

## Pipeline

```
rest_api_fetcher (text)  → regex_parser (split)
                         → regex_parser (extract)  → filter
                         → dataframe_to_csv
```

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `rest_api_fetcher` | ingestion | GET `hnrss.org/frontpage`; `output_format: text` returns raw XML |
| 2 | `regex_parser` (split) | transformation | Split on `</item>`; one row per feed item |
| 3 | `regex_parser` (extract) | transformation | Extract title + link via two capture groups in one regex |
| 4 | `filter` | transformation | Drop rows where `title` is null (the preamble before the first item) |
| 5 | `dataframe_to_csv` | sink | Write title, link |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_hn_rss_demo.sh | bash
cd hn-rss-demo
uv run dg launch --assets '*'
```

## Output

`/tmp/hn_frontpage.csv` — top stories from the live HN front-page feed:

```
title,link
Police Have Used License Plate Readers...,https://ij.org/...
Uber Torches 2026 AI Budget on Claude Code...,https://briefs.co/...
An open letter asking NHS England to keep its code open,https://keepthingsopen.com
```

## What this demo shows

- **First parsing demo.** `regex_parser` is used twice — once in
  `mode: split` (explode rows by a separator regex) and once in
  `mode: extract` (capture-group → new columns).
- **Same component installed twice in one project.** `regex_parser` lives
  at `defs/regex_parser/` for the split step and `defs/regex_extract/` for
  the extraction step (via `dagster-component add ... --target-dir`).
  Each gets its own `defs.yaml`, both pointing at the same Python class.
- **`output_format: text`** on rest_api_fetcher wraps the raw response
  in a 1-row DataFrame so downstream parsers have a column to operate on
  — the same pattern the books-scraper demo uses.

## Extending

Add a `datetime_parser` to parse `<pubDate>...</pubDate>`, then a `sort`
by date. Or chain a `filter` for HN posts above a points threshold.
