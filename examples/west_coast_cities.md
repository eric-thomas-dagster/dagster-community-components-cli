# West-coast cities demo


Same 10-city CSV from the cities_distance demo, but this time the
bounding_box_filter component keeps only cities west of lng -100 and
south of lat 38 (loosely the US west coast / Sun Belt). The output
is a CSV of just those cities.

Pipeline (3 components, all autoloaded by `dg`):
    csv_file_ingestion → bounding_box_filter → dataframe_to_csv

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_west_coast_cities_demo.sh | bash
cd west-coast-cities-demo
uv run dg launch --assets '*'
```
