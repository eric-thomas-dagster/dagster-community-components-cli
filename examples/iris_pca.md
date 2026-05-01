# Iris — PCA dimensionality reduction

A 3-component pipeline that takes Iris's 4-dimensional feature space and
collapses it to 2 principal components, producing a 2D representation that
explains ~95% of the variance.

## Pipeline

```
csv_file_ingestion → pca → dataframe_to_csv
```

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `csv_file_ingestion` | ingestion | Pull Iris CSV (150 flowers × 4 features + species) |
| 2 | `pca` | analytics | Standardize features, project onto top 2 principal components |
| 3 | `dataframe_to_csv` | sink | Write `species`, `pc_1`, `pc_2` |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_iris_pca_demo.sh | bash
cd iris-pca-demo
uv run dg launch --assets '*'
```

## Output

`/tmp/iris_pca.csv`:

```
species,pc_1,pc_2
setosa,-2.265,0.480
setosa,-2.081,-0.674
setosa,-2.364,-0.342
...
versicolor,1.103,0.863
virginica,1.870,0.388
```

PC1 cleanly separates setosa (negative) from virginica (positive);
versicolor sits in the middle. Two components retain ~95% of the original
variance, suitable for plotting.

## What this demo shows

- **Dimensionality reduction in one defs.yaml.** `pca` takes the feature
  list, the number of components, and an `output_prefix`. Result columns
  are `pc_1`, `pc_2`, ... — drop them straight into a scatterplot or
  feed them to a downstream classifier as denser features.
- **`normalize: true`** — runs StandardScaler before fitting PCA, which
  is the right default since PCA is variance-sensitive and unscaled
  features dominate.
- **`include_explained_variance: true`** — the per-component variance
  ratio shows up in the asset's Dagster metadata. Useful for picking
  `n_components` (drop more once cumulative variance plateaus).
