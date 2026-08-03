# Nearest-neighbors demo

Reuses the 10-city CSV from the distance demo, but instead of an
all-pairs cross-join + filter, runs `nearest_neighbors` directly: each
row gets its 3 closest-cities indices and distances added as columns.

Pipeline (3 components, all autoloaded by `dg`):
    file_ingestion → nearest_neighbors → dataframe_to_csv

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `file_ingestion` | ingestion | Read source CSV |
| 2 | `nearest_neighbors` | analytics | sklearn KD-tree neighbors |
| 3 | `dataframe_to_csv` | sink | Write CSV |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_cities_nn_demo.sh | bash
cd cities-nn-demo
uv run dg launch --assets '*'
```

## See also

<!-- TODO: link related walkthroughs -->
