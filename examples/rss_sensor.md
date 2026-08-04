# RSS Sensor demo
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

rss_feed_sensor polls Hacker News' frontpage RSS every 10 minutes. When new
entries appear, it triggers a RunRequest that materializes the
`latest_news_summary` asset, which fetches the same feed via
rest_api_fetcher, parses the headlines via xml_parser, and writes a CSV.

Pipeline (4 components, all autoloaded by `dg`):
  rss_feed_sensor (sensor) ─⟶ triggers ─⟶
      rest_api_fetcher → xml_parser → dataframe_to_csv

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `rest_api_fetcher` | ingestion | Hit a REST endpoint |
| 2 | `xml_parser` | transformation | XPath extract → columns |
| 3 | `dataframe_to_csv` | sink | Write CSV |
| 4 | `rss_feed_sensor` | sensor | Trigger on new RSS entry |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_rss_sensor_demo.sh | bash
cd rss-sensor-demo
uv run dg launch --assets '*'
```

## See also

<!-- TODO: link related walkthroughs -->
