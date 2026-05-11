# Web scraper demo

Fetches a Wikipedia page (raw HTML, no API), extracts every <table> on
the page, picks the first big one, writes it to CSV. Demonstrates the
fetch-HTML → parse-tables → land structured-data pattern entirely from
registry components — a common Dagster use case.

Pipeline (4 components, all autoloaded by `dg`):
    rest_api_fetcher (text) → html_parser (extract_tables)
                            → array_exploder → dataframe_to_json

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | [`rest_api_fetcher`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/rest_api_fetcher) | ingestion | Hit a REST endpoint |
| 2 | [`html_parser`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/html_parser) | transformation | Parse HTML tables |
| 3 | [`array_exploder`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/array_exploder) | transformation |  |
| 4 | [`dataframe_to_json`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_json) | sink |  |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_wiki_scraper_demo.sh | bash
cd wiki-scraper-demo
uv run dg launch --assets '*'
```
