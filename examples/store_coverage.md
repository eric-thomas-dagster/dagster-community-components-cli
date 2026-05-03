# Store Coverage demo


Builds service-area zones around 5 retail stores, finds which of 100
customers fall inside each zone, computes per-store coverage stats,
and tiles the area into a 50km grid for heatmap-style aggregation.

Pipeline (9 components, all autoloaded by `dg`):
  csv_file_ingestion (stores)    → create_points → buffer → smooth
                                                             │
  csv_file_ingestion (customers) → create_points              ├─→ spatial_join → summarize → CSV
                                                             │
                                         → make_grid (heatmap tiles)              → CSV

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_store_coverage_demo.sh | bash
cd store-coverage-demo
uv run dg launch --assets '*'
```
