# REST Countries — formula + summarize + JSON

A 4-component pipeline that pulls every country from the REST Countries API,
computes population density per country, rolls up by region, writes a JSON
report.

## Pipeline

```
rest_api_fetcher → formula → summarize → dataframe_to_json
```

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `rest_api_fetcher` | ingestion | GET `restcountries.com/v3.1/all?fields=region,subregion,population,area,cca3` |
| 2 | `formula` | transformation | Compute `density_per_km2 = population / area` via `df.eval` |
| 3 | `summarize` | transformation | Group by `region`, sum population, mean density, count countries |
| 4 | `dataframe_to_json` | sink | Write `/tmp/region_summary.json` |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_countries_demo.sh | bash
cd countries-demo
uv run dg launch --assets '*'
```

## Output

```json
[
  {"region":"Africa",   "population":1462464411, "density_per_km2":120.27,  "cca3":59},
  {"region":"Americas", "population":1042579783, "density_per_km2":197.93,  "cca3":56},
  {"region":"Asia",     "population":4724731966, "density_per_km2":1030.36, "cca3":50},
  {"region":"Europe",   "population":741657922,  "density_per_km2":687.62,  "cca3":53},
  {"region":"Oceania",  "population":48059678,   "density_per_km2":114.50,  "cca3":27}
]
```

250 countries → 6 regions.

## What this demo shows

- `formula` accepts arbitrary `df.eval` expressions — full pandas semantics
  including operator precedence and column references.
- `summarize` takes a `group_by` list plus an `aggregations` dict mapping
  output column → reducer name.
- The pipeline is fully declarative: zero custom Python beyond
  `model_validate({...})` calls.
