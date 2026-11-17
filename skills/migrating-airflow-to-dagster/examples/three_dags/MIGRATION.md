# Worked example: three DAGs from Airflow to Dagster

Source DAGs are in `airflow_src/`; the Dagster definitions are in `dagster_defs/`; `manifest.json` is the completed manifest (11 units, all complete). Verified with Dagster 1.13.30.

How it was done: `inventory.py` → plan each unit's `target` → write the definitions into a project made with `uvx create-dagster project` (files under `src/<pkg>/defs/`) → `dg check defs` → `validate_defs.py --advance` → `parity.py` and `automation_check.py` for the judgment units → `status.py advance` with evidence → `status.py summary` exits 0.

## Equivalence rows

| Source | Airflow spelling | Dagster spelling | Delta |
|---|---|---|---|
| simple_sequential_pipeline trigger | `schedule="0 9 * * *"`, `catchup=False` | `ScheduleDefinition(cron_schedule="0 9 * * *", execution_timezone="UTC")` over the group's job | Airflow's run for a cron tick covers the data interval that just ended; a Dagster tick has no interval. Unaffected here because the tasks use no interval values. Dagster's treatment of ticks missed while the daemon is down differs from `catchup=False`: confirm before cutover. |
| producer_pipeline trigger | `schedule="@daily"` | cron `0 0 * * *`, UTC | Same as above. |
| consumer_pipeline trigger | `schedule=[processed_data_dataset]` | `AutomationCondition.eager()` on both consumer assets, with `deps` on the producer's dataset asset | Checked with `dg.evaluate_automation_conditions`: idle when nothing has run, idle when the producer ran without updating the dataset, requested when the dataset asset updated. Differences: the condition is evaluated by the automation-condition sensor, which must be enabled, so the trigger is polled (about every 30 s) rather than event-driven; `eager()` also materializes an asset that becomes missing once its deps exist, so deploying the consumer after the dataset already exists can fire it once; neither Airflow nor this check covers a consumer waiting on several datasets. |
| default_args retries | `retries=3`, `retry_delay=5 min` | `RetryPolicy(max_retries=3, delay=300)` on each asset | Same count; Dagster retries the failed asset, not a task instance. |
| XCom (returns passed between tasks) | implicit via TaskFlow arguments | Asset parameters loaded through the default IO manager | The default IO manager writes to local storage. If steps run in different processes or pods (Kubernetes, Hybrid), configure a shared IO manager first or the downstream asset cannot load its input. |
| Datasets | `Dataset("s3://bucket/processed_data/")` | `AssetKey(["s3", "bucket", "processed_data"])` | The key is a name, not a storage binding; the asset still has to write the data. |

## Findings about the source

- The producer and consumer DAGs pass `outlets=[...]` to `@dag`. Airflow 2.10.5's `DAG` rejects that keyword (`TypeError`, tested directly against `DAG(...)`), so these files would not load in real Airflow as written. `outlets` is a task argument. The conversion follows the evident intent: the last producer task writes the dataset.
- The task bodies print and return small dicts; there is no real storage I/O to port.

## What this example does not prove

`parity.py` runs the original task bodies through a stub `airflow` module and compares return values with the Dagster outputs. It checks task logic and data passing, not Airflow's scheduler, XCom serialization or triggering. Operator-based DAGs (BashOperator, sensors, providers) need a different parity approach (see `reference/parity.md`).
