#!/usr/bin/env bash
# Web scraper demo — canonical create-dagster + dg.
#
# Fetches a Wikipedia page (raw HTML, no API), extracts every <table> on
# the page, picks the first big one, writes it to CSV. Demonstrates the
# fetch-HTML → parse-tables → land structured-data pattern entirely from
# registry components — a common Dagster use case.
#
# Pipeline (4 components, all autoloaded by `dg`):
#     rest_api_fetcher (text) → html_parser (extract_tables)
#                             → array_exploder → dataframe_to_json

set -euo pipefail

PROJECT_DIR="${1:-wiki-scraper-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas requests beautifulsoup4 lxml
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 4 community components into src/$PKG/defs/"
$CLI add rest_api_fetcher    --auto-install
$CLI add html_parser         --auto-install
$CLI add array_exploder      --auto-install
$CLI add dataframe_to_json   --auto-install

echo ">>> Writing demo defs.yaml for each component"

# 1. Fetch the Wikipedia page as raw HTML — output_format=text wraps it in
# a 1-row DataFrame with a `content` column. Wikipedia blocks the default
# python-requests UA, so identify ourselves.
cat > "src/$PKG/defs/rest_api_fetcher/defs.yaml" <<EOF
type: $PKG.defs.rest_api_fetcher.component.RestApiFetcherComponent
attributes:
  asset_name: page_html
  api_url: https://en.wikipedia.org/wiki/List_of_countries_by_GDP_(nominal)
  method: GET
  auth_type: none
  output_format: text
  headers: '{"User-Agent": "dagster-community-components-demo/0.3"}'
  description: Wikipedia page — list of countries by GDP (raw HTML)
  group_name: ingest
EOF

# 2. Parse all <table> elements — extract_tables returns a list of tables,
# each table is a list of rows, each row is a list of cells.
cat > "src/$PKG/defs/html_parser/defs.yaml" <<EOF
type: $PKG.defs.html_parser.component.HtmlParserComponent
attributes:
  asset_name: page_tables
  upstream_asset_key: page_html
  columns: [content]
  mode: extract_tables
  parser: html.parser
  group_name: scrape
EOF

# 3. Explode the list-of-tables into one row per table (then we can pick the
# largest one downstream). array_exploder turns list-cell rows into multi-row.
cat > "src/$PKG/defs/array_exploder/defs.yaml" <<EOF
type: $PKG.defs.array_exploder.component.ArrayExploderComponent
attributes:
  asset_name: tables_exploded
  upstream_asset_key: page_tables
  column: content
  ignore_index: true
  drop_nulls: true
  group_name: scrape
EOF

# 4. Write the parsed tables as JSON (each row = one table = list-of-rows)
cat > "src/$PKG/defs/dataframe_to_json/defs.yaml" <<EOF
type: $PKG.defs.dataframe_to_json.component.DataframeToJsonComponent
attributes:
  asset_name: page_tables_report
  upstream_asset_key: tables_exploded
  file_path: /tmp/wiki_tables.json
  orient: records
  indent: 2
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Materialize headlessly:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Or open the UI:
    cd $PROJECT_DIR && uv run dg dev

Output: /tmp/wiki_tables.json — every <table> on the Wikipedia "List of
countries by GDP (nominal)" page, parsed into structured rows-of-cells.

Inspect:
    uv run python -c "
    import json
    tables = json.load(open('/tmp/wiki_tables.json'))
    print(f'Found {len(tables)} tables')
    biggest = max(tables, key=lambda t: len(t['table']))
    print(f'Biggest table: {len(biggest[\"table\"])} rows')
    for row in biggest['table'][:5]:
        print(row)
    "
MSG
