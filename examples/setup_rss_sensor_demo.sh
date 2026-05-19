#!/usr/bin/env bash
# RSS Sensor demo — sensor-driven asset materialization with NO auth needed.
#
# rss_feed_sensor polls Hacker News' frontpage RSS every 10 minutes. When new
# entries appear, it triggers a RunRequest that materializes the
# `latest_news_summary` asset, which fetches the same feed via
# rest_api_fetcher, parses the headlines via xml_parser, and writes a CSV.
#
# Pipeline (4 components, all autoloaded by `dg`):
#   rss_feed_sensor (sensor) ─⟶ triggers ─⟶
#       rest_api_fetcher → xml_parser → dataframe_to_csv

set -euo pipefail
PROJECT_DIR="${1:-rss-sensor-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding deps"
uv add -q pandas requests feedparser
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component --refresh"

echo ">>> Installing 4 community components"
$CLI add rest_api_fetcher  --auto-install
$CLI add xml_parser        --auto-install
$CLI add dataframe_to_csv  --auto-install
$CLI add rss_feed_sensor   --auto-install

echo ">>> Writing demo defs.yaml"

cat > "src/$PKG/defs/rest_api_fetcher/defs.yaml" <<EOF
type: $PKG.components.rest_api_fetcher.component.RestApiFetcherComponent
attributes:
  asset_name: hn_feed_xml
  api_url: "https://hnrss.org/frontpage"
  method: GET
  auth_type: none
  output_format: text
  description: Hacker News frontpage RSS XML
  group_name: ingest
EOF

cat > "src/$PKG/defs/xml_parser/defs.yaml" <<EOF
type: $PKG.components.xml_parser.component.XmlParser
attributes:
  asset_name: latest_news_summary
  upstream_asset_key: hn_feed_xml
  xml_column: content
  mode: findall
  xpath_expressions:
    title: ".//item/title"
    link: ".//item/link"
    pub_date: ".//item/pubDate"
  group_name: transform
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: news_csv
  upstream_asset_key: latest_news_summary
  file_path: /tmp/hn_news.csv
  include_index: false
  group_name: sink
EOF

cat > "src/$PKG/defs/rss_feed_sensor/defs.yaml" <<EOF
type: $PKG.components.rss_feed_sensor.component.RssFeedSensorComponent
attributes:
  sensor_name: hn_top_sensor
  asset_keys: [latest_news_summary]
  feed_url: "https://hnrss.org/frontpage"
  max_entries_per_tick: 5
  minimum_interval_seconds: 60
  default_status: STOPPED
EOF

cat <<MSG

>>> Setup complete.

Materialize the asset graph once (manual seed):
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Then start the sensor in the UI:
    cd $PROJECT_DIR && uv run dg dev
    # Navigate to "Sensors" → enable "hn_top_sensor". It polls every
    # minute and triggers a re-materialization whenever new HN frontpage
    # entries appear. No auth required — public RSS.

Output: /tmp/hn_news.csv — title / link / pubDate of HN frontpage entries.
MSG
