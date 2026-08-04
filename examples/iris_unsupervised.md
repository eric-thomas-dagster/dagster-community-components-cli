# Iris Unsupervised demo
> ✅ **Dagster+ Serverless:** deploys as-is via `dagster-cloud serverless deploy-docker`.

Replaces the separate iris_clusters and iris_pca demos with one richer
pipeline that runs both algorithms in sequence:
  - Standard-scale the 4 numeric features
  - PCA-project to 2D for visualization
  - K-Means cluster on the SCALED features (not the PCA projection — better
    when the original features are well-conditioned)
  - Output a single DataFrame with both PC coordinates + cluster assignment

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `file_ingestion` | ingestion | Read source CSV |
| 2 | `feature_scaler` | transformation | Standardize features |
| 3 | `pca` | analytics | PCA dimensionality reduction |
| 4 | `k_means_clustering` | analytics | K-Means clustering |
| 5 | `dataframe_to_csv` | sink | Write CSV |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_iris_unsupervised_demo.sh | bash
cd iris-unsupervised-demo
uv run dg launch --assets '*'
```

## See also

<!-- TODO: link related walkthroughs -->
