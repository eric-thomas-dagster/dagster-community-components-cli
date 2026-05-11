# NBA Scoreboard

> **Heads-up.** This demo hits a public, undocumented NBA **JSON endpoint** (not
> HTML scraping — the same JSON the NBA's own mobile apps consume):
> `https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json`.

The NBA's public scoreboard JSON includes server timestamps that change
on every poll. A naive whole-body hash would fire the sensor every
minute. Instead, http_poll_sensor uses `json_path` to hash only the
game date + a derived game count — fires only when something
meaningful changes.

Pipeline (4 components, all autoloaded by `dg`):
  http_poll_sensor (sensor) ─⟶ triggers ─⟶
      rest_api_fetcher → json_path_extractor → dataframe_to_csv

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | [`rest_api_fetcher`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/rest_api_fetcher) | ingestion | Hit a REST endpoint |
| 2 | [`json_path_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/json_path_extractor) | transformation | JSONPath extract → columns |
| 3 | [`dataframe_to_csv`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_csv) | sink | Write CSV |
| 4 | [`http_poll_sensor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/sensors/http_poll_sensor) | sensor | Trigger on URL change |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_nba_scoreboard_demo.sh | bash
cd nba-scoreboard-demo
uv run dg launch --assets '*'
```
