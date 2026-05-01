# Iris — k-means clustering

A 4-component pipeline that takes the canonical Iris dataset, standardizes
the four numeric features, runs k-means with k=3, and writes per-flower
cluster assignments + distance-to-centroid to CSV.

## Pipeline

```
csv_file_ingestion → feature_scaler → k_means_clustering → dataframe_to_csv
```

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `csv_file_ingestion` | ingestion | Pull Iris CSV (150 flowers, 3 species, 4 numeric features) |
| 2 | `feature_scaler` | transformation | StandardScaler the four measurements (zero mean, unit variance) |
| 3 | `k_means_clustering` | analytics | Fit k=3 clusters; write `cluster` (0/1/2) + `distance` columns |
| 4 | `dataframe_to_csv` | sink | Write all 150 flowers + their cluster |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_iris_clusters_demo.sh | bash
cd iris-clusters-demo
uv run dg launch --assets '*'
```

## Output

`/tmp/iris_clusters.csv` — 150 rows, every Iris flower with its cluster
assignment and distance to the assigned centroid.

```bash
uv run python -c "
import pandas as pd
df = pd.read_csv('/tmp/iris_clusters.csv')
print(pd.crosstab(df.species, df.cluster, margins=True))
"
```

```
cluster      0   1   2  All
species                    
setosa       0  33  17   50
versicolor  46   0   4   50
virginica   50   0   0   50
```

The model finds three groupings; since clustering is unsupervised, the
cluster IDs (0/1/2) don't match species labels but the partitioning lines
up with the natural species boundaries.

## What this demo shows

- **Unsupervised analytics from one defs.yaml.** `k_means_clustering`
  takes feature columns + k, fits sklearn's `KMeans`, returns the input
  frame with new `cluster` and (optionally) `distance` columns.
- **Pre-scaled features.** `feature_scaler` runs upstream with
  `strategy: standard`. The clusterer then sets `normalize: false` to
  avoid double-scaling.
- **`include_distance: true`** — adds a per-row distance to the assigned
  centroid, useful for finding outliers or "soft" cluster members near
  boundaries.
