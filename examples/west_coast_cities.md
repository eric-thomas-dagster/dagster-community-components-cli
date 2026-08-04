# West-coast cities demo
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

Same 10-city CSV from the cities_distance demo, but this time the
bounding_box_filter component keeps only cities west of lng -100 and
south of lat 38 (loosely the US west coast / Sun Belt). The output
is a CSV of just those cities.

Pipeline (3 components, all autoloaded by `dg`):
    file_ingestion → bounding_box_filter → dataframe_to_csv

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `file_ingestion` | ingestion | Read source CSV |
| 2 | `bounding_box_filter` | analytics | Filter to lat/lng bbox |
| 3 | `dataframe_to_csv` | sink | Write CSV |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_west_coast_cities_demo.sh | bash
cd west-coast-cities-demo
uv run dg launch --assets '*'
```

## See also

<!-- TODO: link related walkthroughs -->
