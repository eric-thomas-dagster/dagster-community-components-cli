# Wine Quality — train a random forest, predict + rank features

A 5-component pipeline that trains a random-forest regressor on the UCI red
wine quality dataset, then emits two parallel outputs — per-row predictions
and a feature-importance ranking — from one shared source asset.

## Pipeline

```
                       ┌─→ random_forest_model (predictions)        → CSV
    csv_file_ingestion ┤
                       └─→ random_forest_model (feature_importance) → CSV
```

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `csv_file_ingestion` | ingestion | Load the UCI red wine CSV (semicolon-separated, 1599 rows × 12 cols) |
| 2 | `random_forest_model` (predictions branch) | analytics | Train a regressor on `quality`; output the input df with a new `predicted` column |
| 3 | `random_forest_model` (importance branch) | analytics | Same training, different output — a tiny df with feature names and importance scores |
| 4 | `dataframe_to_csv` × 2 | sink | Write each branch to its own CSV |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_wine_demo.sh | bash
cd wine-demo
uv run dg launch --assets '*'
```

## Outputs

`/tmp/wine_predictions.csv` — every wine plus its model-predicted quality:

```
fixed acidity,volatile acidity, ... ,quality,predicted
7.4,0.7, ... ,5,5.101
7.8,0.88, ... ,5,5.180
```

`/tmp/wine_feature_importance.csv` — features ranked by predictive power:

```
feature,importance
alcohol,0.300
sulphates,0.160
volatile acidity,0.110
total sulfur dioxide,0.075
...
```

## What this demo shows

- **The same component, used twice in one pipeline** — `random_forest_model`
  is installed once via `dagster-component add`, then a second time with
  `--target-dir` into a separate `defs/` subdir. Each gets its own
  `defs.yaml` (one per output mode), and `dg`'s autoloader treats them as
  two distinct assets fed by the same upstream.
- **`output_mode: predictions` vs `feature_importance`** — both modes share
  the same training step internally; the asset's output dataframe shape
  differs by mode.
- **A real ML pipeline with no glue code** — train/test split, sklearn
  fit/predict, R² scoring as metadata, all from one `defs.yaml`. No
  `definitions.py`, no `importlib.util`, no Python beyond what the
  components already ship.

## Extending

Swap `random_forest_model` for `gradient_boosting_model`, `decision_tree_model`,
`linear_regression_model`, or `neural_network_model` — same field shape, same
output modes. Or change `task_type: regression` to `classification` if your
target is categorical.
