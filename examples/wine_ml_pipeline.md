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

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_wine_ml_pipeline_demo.sh | bash
cd wine-ml-pipeline-demo
uv run dg launch --assets '*'
```
