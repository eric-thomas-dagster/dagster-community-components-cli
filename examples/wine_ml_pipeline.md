# Wine ML Pipeline demo

Builds a more substantial ML pipeline than the existing wine_demo:
  - feature scaling (standardize the 11 chemistry features)
  - train/test split via create_samples
  - decision_tree_model with two parallel outputs (predictions + feature_importance)
  - cross_validation to verify model stability across folds
  - three CSV sinks

Pipeline (8 components, all autoloaded by `dg`):
                         ┌─→ create_samples ─┐
                         │                    ├─→ decision_tree (predictions)        → CSV
    csv_file_ingestion ─→ feature_scaler ──┐ │
                         │                  ├┴→ decision_tree (feature_importance)   → CSV
                         └────────────────→ cross_validation                          → CSV

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | [`csv_file_ingestion`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/csv_file_ingestion) | ingestion | Read source CSV |
| 2 | [`feature_scaler`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/feature_scaler) | transformation | Standardize features |
| 3 | [`create_samples`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/create_samples) | transformation | Train/test split |
| 4 | [`decision_tree_model`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/decision_tree_model) | analytics | Fit decision tree |
| 5 | [`cross_validation`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/cross_validation) | analytics | k-fold CV scores |
| 6 | [`dataframe_to_csv`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_csv) | sink | Write CSV |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_wine_ml_pipeline_demo.sh | bash
cd wine-ml-pipeline-demo
uv run dg launch --assets '*'
```
