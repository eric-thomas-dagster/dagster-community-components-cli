# MLflow pipeline — end-to-end MLOps with Dagster

> ✅ **Dagster+ Serverless:** deploys as-is via `dagster-cloud serverless deploy-docker`. Sqlite-backed MLflow lives inside the container — self-contained.

Self-contained demo of Dagster orchestrating a full MLflow-backed ML pipeline. Trains a churn model, registers it, promotes Staging → Production as a Dagster asset, then scores customer data using the promoted model — with an asset check gating on model existence.

Uses **all 7 community MLflow components**:

| Component | Role in this demo |
|---|---|
| `mlflow_resource` | Shared MLflow connection (available for downstream ops if needed) |
| `mlflow_workspace` | Auto-enumerates the `churn_model` registry entry + experiment as read-only Dagster assets |
| `mlflow_experiment_sensor` | (Ready to wire) fires downstream when a new experiment run finishes |
| `mlflow_model_sensor` | (Ready to wire) fires downstream when a new Production version appears |
| `mlflow_model_version_check` | Asset check — "must have a Production `churn_model`" gate on the scoring asset |
| `mlflow_model_promotion` | Materializable asset: transitions v1 Staging → Production; auditable ML CD step |
| `mlflow_model_inference` | Materializable asset: loads the Production model, scores 20 customers |

**Setup:**

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_mlflow_pipeline_demo.sh \
  -o setup_mlflow_pipeline_demo.sh
bash setup_mlflow_pipeline_demo.sh
cd mlflow_demo
export MLFLOW_TRACKING_URI="sqlite:///$(pwd)/mlflow.db"
uv run dg dev            # → http://localhost:3000
```

Requirements: [uv](https://docs.astral.sh/uv/). Cost: $0. Runs against a self-contained sqlite MLflow backend inside the project dir.

## What the script does

1. Scaffolds a Dagster project (`create-dagster`)
2. Installs `dagster-community-components`, `mlflow`, `scikit-learn`, `pandas`
3. Trains a `LogisticRegression` churn model on 500 rows of synthetic data
4. Logs the training run (metrics + params) to MLflow → registers as `churn_model` v1
5. Transitions v1 to `Staging`
6. Writes a 20-row `customer_features.csv` (the batch to score)
7. Drops 5 `defs.yaml` files wiring the 4 workflow + 1 workspace components

## The pipeline shape

```
                           ┌──────────────────────┐
                           │ mlflow_workspace     │
                           │ (read-only registry  │
                           │  enumeration)        │
                           └──────────────────────┘

┌──────────────────┐       ┌──────────────────────┐
│ customer_features│       │ promote_churn_to_    │
│  (from CSV)      │       │  production          │
└────────┬─────────┘       │  (Staging → Prod)    │
         │                 └──────────┬───────────┘
         │                            │
         └────────────┬───────────────┘
                      ▼
           ┌────────────────────────┐
           │ daily_churn_scores     │
           │ (loads Production      │
           │  model, scores rows)   │
           └────────┬───────────────┘
                    │
                    ▼
           ┌────────────────────────────────┐
           │ ASSET CHECK:                   │
           │  churn_production_model_exists │
           │  (blocks if no Prod model)     │
           └────────────────────────────────┘
```

## Verified end-to-end

Actual test-run log from validation:

```
▸ promote_churn_to_production — Transitioned churn_model v1: Staging → Production
▸ customer_features — Materialized (20 rows)
▸ daily_churn_scores — Loading model models:/churn_model/Production ... Materialized
▸ churn_production_model_exists — PASSED: Model 'churn_model' check passed: version 1 at stage 'Production'
▸ RUN_SUCCESS
```

## Run headless

```bash
cd mlflow_demo
export MLFLOW_TRACKING_URI="sqlite:///$(pwd)/mlflow.db"
uv run dg launch --assets 'customer_features,promote_churn_to_production,daily_churn_scores'
```

On subsequent runs (v1 already at Production), the promotion asset raises "no versions at Staging" — expected. To demo again, re-log a new training run first (or drop `mlflow.db` and re-run the setup script).

## How this fits with official `dagster-mlflow`

**Official [`dagster-mlflow`](https://docs.dagster.io/integrations/libraries/mlflow/dagster-mlflow)** — tracking data FLOWS INTO MLflow from Dagster during training:
- `mlflow_tracking` resource — initializes MLflow run for every Dagster run
- `end_mlflow_on_run_finished` hook — cleanly closes it
- Use in training assets that call `mlflow.log_metric()` etc.

**Community components (this demo)** — Dagster OBSERVES + ACTS on MLflow state:
- Enumerate registry as assets (`mlflow_workspace`)
- React to registry changes (`mlflow_model_sensor`, `mlflow_experiment_sensor`)
- Gate downstream on registry state (`mlflow_model_version_check`)
- Take actions in the registry (`mlflow_model_promotion`)
- Score with registered models (`mlflow_model_inference`)

Different direction of the arrow. A real MLflow-heavy project uses **both** together — `dagster-mlflow` during training runs, community components everywhere else.

## Extending the demo

**Add the experiment sensor** — trigger evaluation whenever a new training run completes:

```yaml
# src/mlflow_demo/defs/experiment_sensor/defs.yaml
type: dagster_community_components.MLflowExperimentSensorComponent
attributes:
  sensor_name: new_training_runs
  tracking_uri_env_var: MLFLOW_TRACKING_URI
  experiment_name: churn_prediction
  filter_string: "attributes.status = 'FINISHED'"
  target_job: __ASSET_JOB   # or your own evaluation job
```

**Add the model sensor** — trigger scoring whenever a new Production version lands:

```yaml
# src/mlflow_demo/defs/model_sensor/defs.yaml
type: dagster_community_components.MLflowModelSensor
attributes:
  sensor_name: new_prod_model
  tracking_uri_env_var: MLFLOW_TRACKING_URI
  model_name: churn_model
  target_stage: Production
  target_job: __ASSET_JOB
```

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_mlflow_pipeline_demo.sh \
  -o setup_mlflow_pipeline_demo.sh
bash setup_mlflow_pipeline_demo.sh
```

## See also

- Official [`dagster-mlflow`](https://docs.dagster.io/integrations/libraries/mlflow/dagster-mlflow) — the tracking side
- [dbt_ml_pipeline](dbt_ml_pipeline.md) — flagship dbt + ML + dbt "why Dagster over Airflow"
- [dbt_queue_driven](dbt_queue_driven.md) — message-driven dbt orchestration
