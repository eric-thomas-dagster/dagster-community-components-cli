# Parity checks

A unit that loads and matches its plan (`validate_defs.py`) is structurally right. Parity checks are what justify advancing a judgment unit. Two checks were run and verified in `examples/three_dags/` (Dagster 1.13.30):

## 1. Function-level parity (TaskFlow and PythonOperator tasks)

Run the original task bodies without Airflow and compare return values with the materialized Dagster assets.

- Stub `airflow` / `airflow.decorators` so `@task` is a pass-through that records each return value (key: module plus function name; two DAGs can both define `process_data`), and `@dag(...)` returns the function unchanged. Load the original files with `importlib`.
- Materialize the Dagster assets in-process with `dg.materialize([...])` and read each output with `result.asset_value(asset.key)`.
- Compare for equality.

This checks logic and data passing. It does not check Airflow's scheduling, XCom serialization, retries or triggers. See `examples/three_dags/parity.py`.

## 2. Trigger parity (dataset schedules, sensors via automation)

`dg.evaluate_automation_conditions(defs=..., instance=..., cursor=...)` evaluates declarative automation in-process; `result.get_num_requested(asset_key)` says what would be launched. Drive it by materializing upstream assets between evaluations and pass the previous `result.cursor`. Check at least: nothing runs when nothing has happened; nothing runs when upstream ran but the dataset was not updated; the consumer is requested when the dataset updates.

Caveat: the evaluation requests runs but does not launch them, so later ticks in a script that never executes those runs are not a faithful simulation. Interpret only the ticks up to the first request. In production the automation-condition sensor must be enabled. See `examples/three_dags/automation_check.py`.

## What still needs a live comparison

Operator-based tasks, sensors, anything touching external systems, durations, failure behavior and data volume. For those, run both systems on the same logical window and compare outputs (row counts, checksums); with an Airflow instance available, `af runs list` and `af dags stats` give the baseline durations and failure rates.
