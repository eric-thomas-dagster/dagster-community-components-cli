# Web scraper — partitioned multi-page crawl

A 3-component pipeline that scrapes 5 paginated pages of `books.toscrape.com`,
one partition per page. Each partition fetches its page, extracts every link,
and writes a JSON file. Backfill any range of pages with a single command.

## Pipeline

```
rest_api_fetcher (text)  → html_parser (extract_links)  → dataframe_to_json
{partition_key} → URL                                    {partition_key} → file_path
```

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `rest_api_fetcher` | ingestion | GET `/catalogue/page-{partition_key}.html` — `output_format: text` returns raw HTML |
| 2 | `html_parser` | transformation | `mode: extract_links` parses every `<a href>` |
| 3 | `dataframe_to_json` | sink | One file per partition: `/tmp/books_page_{partition_key}.json` |

All three are partitioned with `partition_type: static`, `partition_values: ["1","2","3","4","5"]`.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_books_scraper_demo.sh | bash
cd books-scraper-demo

# One page:
uv run dg launch --assets '*' --partition 1

# All five (one run per partition):
for p in 1 2 3 4 5; do uv run dg launch --assets '*' --partition $p; done

# Or via the UI:
uv run dg dev   # http://localhost:3000 → pick partitions
```

## Output

Five files, one per page:

```
/tmp/books_page_1.json    # page 1 — 60 <a href> URLs
/tmp/books_page_2.json
/tmp/books_page_3.json
/tmp/books_page_4.json
/tmp/books_page_5.json
```

Each file is a JSON array with one record `{"content": [<list of URLs>]}`.

## What this demo shows

- **Partitioned web scraping with no glue code.** The `{partition_key}` URL
  template + static partitions = "scrape this list of pages" expressed as
  YAML. The Dagster UI shows each page as a discrete materializable unit;
  retries, backfills, and parallelism work the same as for any other
  partitioned asset.
- **`output_format: text` on rest_api_fetcher** — wraps raw response body
  in a 1-row `content` column so transforms like `html_parser` and
  `regex_parser` can chain off it.
- **`html_parser` modes** — `extract_links`, `extract_tables`,
  `extract_text`, `strip_tags`. Same component, different downstream
  shapes.

## Extending

Drop a `regex_parser` between html_parser and the sink to extract just
book detail URLs (`/catalogue/<slug>/index.html`), then chain another
fetch+parse pair to scrape each book's detail page. Or swap
`partition_values` for a date-based axis if your target paginates by
date instead of page number.
