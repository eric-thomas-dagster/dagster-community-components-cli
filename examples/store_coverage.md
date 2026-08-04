# Store Coverage demo
> ✅ **Dagster+ Serverless:** deploys as-is via `dagster-cloud serverless deploy-docker`.

Builds service-area zones around 5 retail stores, finds which of 100
customers fall inside each zone, computes per-store coverage stats,
and tiles the area into a 50km grid for heatmap-style aggregation.

Pipeline (9 components, all autoloaded by `dg`):
  file_ingestion (stores)    → create_points → buffer → smooth
                                                             │
  file_ingestion (customers) → create_points              ├─→ spatial_join → summarize → CSV
                                                             │
                                         → make_grid (heatmap tiles)              → CSV

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `file_ingestion` | ingestion | Read source CSV |
| 2 | `create_points` | analytics | lat/lng → shapely Points |
| 3 | `buffer` | analytics | Polygon buffer (radius) |
| 4 | `smooth` | analytics | Simplify geometry |
| 5 | `make_grid` | analytics | Tile bbox into grid cells |
| 6 | `spatial_join` | analytics | Spatial within / contains |
| 7 | `summarize` | transformation | Group-by aggregate |
| 8 | `dataframe_to_csv` | sink | Write CSV |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_store_coverage_demo.sh | bash
cd store-coverage-demo
uv run dg launch --assets '*'
```

## See also

<!-- TODO: link related walkthroughs -->
