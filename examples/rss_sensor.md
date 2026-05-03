# RSS Sensor demo


rss_feed_sensor polls Hacker News' frontpage RSS every 10 minutes. When new
entries appear, it triggers a RunRequest that materializes the
`latest_news_summary` asset, which fetches the same feed via
rest_api_fetcher, parses the headlines via xml_parser, and writes a CSV.

Pipeline (4 components, all autoloaded by `dg`):
  rss_feed_sensor (sensor) ─⟶ triggers ─⟶
      rest_api_fetcher → xml_parser → dataframe_to_csv

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_rss_sensor_demo.sh | bash
cd rss-sensor-demo
uv run dg launch --assets '*'
```
