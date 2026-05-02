#!/usr/bin/env bash
# Partitioned web scraper demo — canonical create-dagster + dg.
#
# Scrapes 5 pages of books.toscrape.com — each page is one static partition.
# Backfilling multiple pages is "materialize this partition range" rather
# than looping in Python. Writes one JSON file per page with extracted links.
#
# Pipeline (3 components, all autoloaded by `dg`, partitioned per page):
#     rest_api_fetcher (text)  → html_parser (extract_links)  → dataframe_to_json
#     {partition_key} → URL                                    {partition_key} → file_path

set -euo pipefail

PROJECT_DIR="${1:-books-scraper-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas requests beautifulsoup4
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 3 community components into src/$PKG/defs/"
$CLI add rest_api_fetcher    --auto-install
$CLI add html_parser         --auto-install
$CLI add dataframe_to_json   --auto-install

echo ">>> Writing demo defs.yaml for each component (5 partitions, pages 1-5)"

# 1. Fetch — URL templated with {partition_key}; static partitions for pages 1-5.
cat > "src/$PKG/defs/rest_api_fetcher/defs.yaml" <<EOF
type: $PKG.components.rest_api_fetcher.component.RestApiFetcherComponent
attributes:
  asset_name: page_html
  api_url: https://books.toscrape.com/catalogue/page-{partition_key}.html
  method: GET
  auth_type: none
  output_format: text
  partition_type: static
  partition_values: ["1", "2", "3", "4", "5"]
  description: books.toscrape.com — paginated catalog, one partition per page
  group_name: ingest
EOF

# 2. Parse — extract every <a href> on the page; result is a list-cell of URLs
cat > "src/$PKG/defs/html_parser/defs.yaml" <<EOF
type: $PKG.components.html_parser.component.HtmlParserComponent
attributes:
  asset_name: page_links
  upstream_asset_key: page_html
  columns: [content]
  mode: extract_links
  parser: html.parser
  partition_type: static
  partition_values: ["1", "2", "3", "4", "5"]
  group_name: scrape
EOF

# 3. Sink — one JSON file per partition (per page)
cat > "src/$PKG/defs/dataframe_to_json/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_json.component.DataframeToJsonComponent
attributes:
  asset_name: page_report
  upstream_asset_key: page_links
  file_path: /tmp/books_page_{partition_key}.json
  orient: records
  indent: 2
  partition_type: static
  partition_values: ["1", "2", "3", "4", "5"]
  group_name: sink
EOF

cat <<MSG

>>> Setup complete. 5 partitions defined (pages 1-5).

Materialize a single page:
    cd $PROJECT_DIR
    uv run dg launch --assets '*' --partition 1

Loop all 5 pages headlessly:
    for p in 1 2 3 4 5; do uv run dg launch --assets '*' --partition \$p; done

Or open the UI and pick partitions there:
    uv run dg dev   # http://localhost:3000

Output: /tmp/books_page_<N>.json (one file per materialized partition).

Inspect:
    cat /tmp/books_page_1.json | head -30
MSG
