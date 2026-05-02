#!/usr/bin/env bash
# RSS parsing demo — canonical create-dagster + dg.
#
# Fetches the Hacker News front-page RSS feed (raw XML), splits it on item
# boundaries via regex, extracts each item's title + link with a regex
# capture-group pair, drops the empty preamble, writes a clean CSV.
#
# Pipeline (5 components, all autoloaded by `dg`):
#     rest_api_fetcher (text)  → regex_parser (split)
#                              → regex_parser (extract)  → filter
#                              → dataframe_to_csv

set -euo pipefail

PROJECT_DIR="${1:-hn-rss-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas requests
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 4 distinct components into src/$PKG/defs/ (regex_parser used twice)"
$CLI add rest_api_fetcher    --auto-install
$CLI add regex_parser        --auto-install
$CLI add filter              --auto-install
$CLI add dataframe_to_csv    --auto-install
# Second regex_parser for the extract step
$CLI add regex_parser        --auto-install --target-dir "src/$PKG/defs/regex_extract"

echo ">>> Writing demo defs.yaml for each component"

# 1. Fetch the RSS feed as raw XML (output_format=text wraps in 1-row df)
cat > "src/$PKG/defs/rest_api_fetcher/defs.yaml" <<EOF
type: $PKG.components.rest_api_fetcher.component.RestApiFetcherComponent
attributes:
  asset_name: feed_xml
  api_url: https://hnrss.org/frontpage
  method: GET
  auth_type: none
  output_format: text
  description: Hacker News RSS — front page (refreshed every few minutes)
  group_name: ingest
EOF

# 2. Split on </item> boundary — each item becomes its own row
cat > "src/$PKG/defs/regex_parser/defs.yaml" <<EOF
type: $PKG.components.regex_parser.component.RegexParser
attributes:
  asset_name: feed_items
  upstream_asset_key: feed_xml
  column: content
  pattern: "</item>"
  mode: split
  group_name: parse
EOF

# 3. Extract <title>...</title> and <link>...</link> in one regex with two capture groups
cat > "src/$PKG/defs/regex_extract/defs.yaml" <<EOF
type: $PKG.components.regex_extract.component.RegexParser
attributes:
  asset_name: feed_extracted
  upstream_asset_key: feed_items
  column: content
  pattern: "<title><!\\\\[CDATA\\\\[(.*?)\\\\]\\\\]></title>.*?<link>(.*?)</link>"
  mode: extract
  output_columns: [title, link]
  flags: 16
  group_name: parse
EOF

# 4. Drop the preamble row + any item where the regex didn't match
cat > "src/$PKG/defs/filter/defs.yaml" <<EOF
type: $PKG.components.filter.component.FilterComponent
attributes:
  asset_name: feed_clean
  upstream_asset_key: feed_extracted
  condition: "title.notna() and title.str.len() > 0"
  group_name: parse
EOF

# 5. Sink — title + link only
cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: feed_report
  upstream_asset_key: feed_clean
  file_path: /tmp/hn_frontpage.csv
  include_index: false
  columns: [title, link]
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Output: /tmp/hn_frontpage.csv — top stories from the Hacker News
front-page feed. Refresh the run any time to get the latest set.

Inspect:
    head -10 /tmp/hn_frontpage.csv
MSG
