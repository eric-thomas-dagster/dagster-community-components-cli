#!/usr/bin/env bash
# RSS parsing demo (xml_parser variant) — canonical create-dagster + dg.
#
# Same goal as the regex-parser HN demo (extract titles + links from the
# Hacker News front-page RSS feed) but routed through the proper XML
# toolchain: xml_parser in `findall` mode → array_exploder explodes
# both columns in parallel → CSV. Xpath all the way down, no regex.
#
# Pipeline (4 components, all autoloaded by `dg`):
#     rest_api_fetcher (text)  → xml_parser (findall)
#                              → array_exploder (parallel)
#                              → dataframe_to_csv

set -euo pipefail

PROJECT_DIR="${1:-hn-xml-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas requests
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 4 components into src/$PKG/defs/"
$CLI add rest_api_fetcher    --auto-install
$CLI add xml_parser          --auto-install
$CLI add array_exploder      --auto-install
$CLI add dataframe_to_csv    --auto-install

echo ">>> Writing demo defs.yaml for each component"

# 1. Fetch HN RSS as raw XML
cat > "src/$PKG/defs/rest_api_fetcher/defs.yaml" <<EOF
type: $PKG.defs.rest_api_fetcher.component.RestApiFetcherComponent
attributes:
  asset_name: feed_xml
  api_url: https://hnrss.org/frontpage
  method: GET
  auth_type: none
  output_format: text
  description: Hacker News RSS — front page (XML)
  group_name: ingest
EOF

# 2. xpath findall — title + link → list-cells (one row, two list columns)
cat > "src/$PKG/defs/xml_parser/defs.yaml" <<EOF
type: $PKG.defs.xml_parser.component.XmlParser
attributes:
  asset_name: feed_lists
  upstream_asset_key: feed_xml
  xml_column: content
  mode: findall
  xpath_expressions:
    title: ".//item/title"
    link: ".//item/link"
  group_name: parse
EOF

# 3. Explode both columns in parallel — pandas zips list-of-lists row-wise
cat > "src/$PKG/defs/array_exploder/defs.yaml" <<EOF
type: $PKG.defs.array_exploder.component.ArrayExploderComponent
attributes:
  asset_name: feed_items
  upstream_asset_key: feed_lists
  column: [title, link]
  ignore_index: true
  drop_nulls: true
  group_name: parse
EOF

# 4. Sink — title + link only
cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.defs.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: feed_report
  upstream_asset_key: feed_items
  file_path: /tmp/hn_xml_frontpage.csv
  include_index: false
  columns: [title, link]
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Output: /tmp/hn_xml_frontpage.csv — same shape as the regex-parser
variant of the HN demo, but extracted via xpath instead of regex.

Inspect:
    head -10 /tmp/hn_xml_frontpage.csv
MSG
