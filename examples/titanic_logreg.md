# Titanic — logistic regression (binary classification)

A 5-component pipeline that predicts Titanic survival from passenger
features. Different ML shape than the wine demo — that's regression
(predict continuous `quality`), this is classification (predict binary
`Survived` 0/1) with class probabilities.

## Pipeline

```
csv_file_ingestion → imputation → one_hot_encoding
                   → logistic_regression_model → dataframe_to_csv
```

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `csv_file_ingestion` | ingestion | Pull Titanic CSV |
| 2 | `imputation` | transformation | Median-fill `Age` and `Fare` (the only numeric NaNs) |
| 3 | `one_hot_encoding` | transformation | Expand `Sex` → `Sex_male` + `Sex_female` |
| 4 | `logistic_regression_model` | analytics | Fit binary classifier, attach `predicted_class` + per-class probabilities |
| 5 | `dataframe_to_csv` | sink | Write the enriched 891-passenger frame |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_titanic_logreg_demo.sh | bash
cd titanic-logreg-demo
uv run dg launch --assets '*'
```

## Output

`/tmp/titanic_predictions.csv` — every passenger plus:

- `predicted_class` — 0 / 1
- `predicted_proba_0` / `predicted_proba_1` — class probabilities

Confusion matrix and accuracy:

```
uv run python -c "
import pandas as pd
df = pd.read_csv('/tmp/titanic_predictions.csv')
print('accuracy:', (df.Survived == df.predicted_class).mean().round(3))
print(pd.crosstab(df.Survived, df.predicted_class, margins=True))
"

accuracy: 0.802
predicted_class    0    1  All
Survived
0                479   70  549
1                106  236  342
All              585  306  891
```

80.2% accuracy with 7 features and no tuning — the classic Titanic
"women and children first" signal dominates.

## What this demo shows

- **Binary classification, not regression.** `logistic_regression_model`
  emits class predictions + per-class probabilities, not continuous
  values. Great for risk scoring, churn prediction, fraud flagging.
- **Components compose by data shape, not API contract.** `imputation`
  → `one_hot_encoding` → `logistic_regression_model` works because each
  emits a DataFrame the next can consume — no special wiring.
- **`output_predictions` + `output_probabilities`** — toggle whether
  the result includes the predicted class (`predicted_class`), the per-
  class probabilities (`predicted_proba_0`, `predicted_proba_1`), or
  both. Probabilities matter for thresholding and calibration.

## Extending

Swap `logistic_regression_model` for `random_forest_model` (set
`task_type: classification`), `gradient_boosting_model`, or
`naive_bayes_model` — same field shape, different decision boundary.
