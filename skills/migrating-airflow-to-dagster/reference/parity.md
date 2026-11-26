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

## 3. Running the DAGs on both sides, incrementally

`scripts/parity_incremental.py CONFIG.json` runs each converted DAG with Airflow's own `dag.test()` and with `dg.materialize`, in separate interpreters, and compares the overall outcome, each task's outcome, and each returned value (`scripts/parity/compare.py`). It records a hash of each DAG's generated module and, on the next run, runs only the DAGs whose hash changed, so a parity pass after a small change takes seconds, not the full corpus time. Results from earlier runs stay in the state directory, so the comparison always covers every DAG that has been run. Pass `--all` after changing something the DAGs share (the generated `runtime.py`); use `--seed` once to record the current conversion as already checked, and `--select` to see what would run. The config format is at the top of the script.

Choose the DAGs to compare with care: converted, no external systems, no long waits (those need a mocked system, as in `examples/runtime_conversion/component_parity/`). Values that are random or time-based will differ and are not defects; for a mapped task Airflow records one instance and Dagster the collected list, so the check passes if Airflow's value appears in the list.

## 4. Three-way behaviour comparison (original, converter, rewrite)

`scripts/eval_behavior.py` runs the original DAG in real Airflow twice (the repeat marks nondeterministic fields) and each route's Dagster output, in identical sandboxes (scratch working directory, SQLite, HTTP and SMTP recorders, moto for S3), and compares outcomes, each task's outcome, XCom against asset values, files, rows, requests sent, mail, S3 objects, failure behaviour, schedule, retries, pools, dependencies and job structure, each dimension on its own, with a roll-up verdict. See [rewrite-route-evaluation-runbook.md](rewrite-route-evaluation-runbook.md). It is the successor to section 3 for the rewrite route.

## What still needs a live comparison

Operator-based tasks, sensors, anything touching external systems, durations, failure behavior and data volume. For those, run both systems on the same logical window and compare outputs (row counts, checksums); with an Airflow instance available, `af runs list` and `af dags stats` give the baseline durations and failure rates.
