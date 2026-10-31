# MLOps model-drift guardrail

A model-serving pipeline shouldn't just score data — it should continuously verify it's running the *right* model AND watch for drift in its own outputs. This demo chains `MLflowModelInferenceComponent`, `MLflowModelVersionCheckComponent`, and `AnomalyDetectionComponent` into one Dagster graph.

```
customer_metrics → churn_scores → churn_score_anomalies
    (synth)      (MLflow scoring)    (anomaly detection)
                       |
                churn_model_prod_exists (asset check)
```

## Run it

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_mlops_drift_guardrail_demo.sh | bash
```

Cost: $0. Everything runs against a real, local, sqlite-backed MLflow model registry — no MLflow server, no cloud account.

## What it does

1. **`synthetic_data_generator`** generates 50 rows of `customer_churn_metrics` (orders, revenue, lifetime days per customer).
2. A small setup script trains a real `sklearn.LogisticRegression` and registers it in a local MLflow registry (`sqlite:///mlflow.db`) at the `Production` stage — before Dagster ever touches it.
3. **`mlflow_model_inference`** loads the Production-staged `churn_model` and scores all 50 customers, producing `churn_scores`.
4. **`mlflow_model_version_check`** is an asset check attached to `churn_scores` — it verifies a `Production` version of `churn_model` actually exists in the registry. This is what would block a downstream job if someone forgot to promote a model.
5. **`anomaly_detection`** runs z-score outlier detection directly on the model's own `churn_score` output — a building block for real drift monitoring (a single run can't detect distributional drift over time, but it proves the check is wired onto the model's own output, ready to run on every batch).

## A real `dg` bug found along the way

A purely-numeric string field value — `model_version: "1"` — gets silently re-coerced to an `int` by `dg`'s own YAML loader before it reaches the component's Pydantic model, regardless of quoting. Confirmed not fixable from the YAML side: plain quotes and an explicit `!!str` YAML tag both failed identically with the same `pydantic_core.ValidationError`. This looks like the same class of bug the [agent memory demo](agent_memory_formation.md) hit with a quoted string literal in a `FormulaComponent` expression — a different symptom (wrong type here, vs. stripped quotes there), but the same underlying "a value that looks like something else gets silently re-typed somewhere in `dg`'s YAML-to-Pydantic pipeline" class of issue.

**Workaround used here:** `model_stage: Production` instead of a bare numeric `model_version` pin, with the model explicitly transitioned to that stage in the setup script. MLflow's model-registry "stages" are deprecated since 2.9 in favor of aliases but remain fully functional (one `FutureWarning`, no error) as of MLflow 3.17.

## Verified

Fresh end-to-end run, byte-for-byte as shipped: `dg check defs` passes, `dg launch --assets '*'` → `RUN_SUCCESS`. The asset check passed (`'churn_model' check passed: version 1 at stage 'Production'`), and anomaly detection flagged 7 of 50 customers (14%) at `threshold: 2.0`.

## Production variants

- Add `max_age_days` to `mlflow_model_version_check` to also gate on model freshness (fail if the Production model hasn't been updated recently).
- Partition `customer_metrics`/`churn_scores` daily to track drift over time instead of a single snapshot.
- Point `tracking_uri_env_var` at a real MLflow tracking server and skip the local setup script entirely.
