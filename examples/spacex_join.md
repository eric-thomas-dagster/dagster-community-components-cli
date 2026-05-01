# SpaceX launches × rockets — multi-source DataFrame join

A 5-component pipeline pulling **two** REST endpoints, joining them on a
foreign-key column, and writing an enriched report. First demo to use
`dataframe_join` and to fan-in two ingest assets into a single transform.

## Pipeline

```
rest_api_fetcher (launches) ┐
                             ├─→ dataframe_join → select_columns → CSV
rest_api_fetcher (rockets)  ┘
```

| # | Component | Category | Role |
|---|---|---|---|
| 1a | `rest_api_fetcher` (launches) | ingestion | GET `/v4/launches` — every SpaceX launch with a `rocket` ID FK |
| 1b | `rest_api_fetcher` (rockets) | ingestion | GET `/v4/rockets` — rocket catalog (Falcon 1 / 9 / Heavy / Starship) |
| 2 | `dataframe_join` | transformation | LEFT JOIN launches.rocket = rockets.id with `_launch` / `_rocket` suffixes |
| 3 | `select_columns` | transformation | Keep + rename to human-readable column names |
| 4 | `dataframe_to_csv` | sink | Write the enriched 205-row table |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_spacex_join_demo.sh | bash
cd spacex-join-demo
uv run dg launch --assets '*'
```

## Output

`/tmp/spacex_with_rockets.csv` — 205 launches enriched with rocket
specs:

```
launch_name,date_utc,success,flight_number,rocket_name,mass,height
FalconSat,2006-03-24T22:30:00.000Z,False,1,Falcon 1,...,...
DemoSat,2007-03-21T01:10:00.000Z,False,2,Falcon 1,...,...
```

Counts per rocket:

```
193 Falcon 9
  5 Falcon Heavy
  5 Falcon 1
```

## What this demo shows

- **Fan-in to a join.** `dataframe_join` has `left_asset_key` +
  `right_asset_key` (instead of the usual single `upstream_asset_key`),
  which `dg`'s autoloader resolves into a two-input asset graph.
- **The same component installed twice in one project.** Both fetches
  use `rest_api_fetcher`; the second is installed with `--target-dir
  src/<pkg>/defs/rest_rockets/` so each gets its own `defs.yaml`. Same
  pattern the wine demo uses for `random_forest_model`.
- **Type field uses the actual class name.** The class is
  `DataframeJoin` (no `Component` suffix) — verify the class name from
  the component.py rather than guessing. The schema-aware YAML
  autocomplete the CLI sets up will catch this if you have the YAML
  language server installed.
