# Specification: Airflow DAG `basic_dataset_schedule_upstream_2`

Facts read from the IR (what Airflow built). Statements marked *inferred* are not facts. No secret value is in this file: credential-like arguments are redacted and connections and Variables are names only.

- file: `2-10-example-dags/datasets/basic_dataset_schedule/basic_dataset_schedule_upstream_2.py`; Airflow 2.10.5; tags: 2-10, Basic Dataset Schedule, Datasets; owners: airflow
- description: none

## What it does (from the facts)

- a 1-task DAG using _PythonDecoratedOperator (the task list)
- its final step(s) are write_to_s3 (tasks with no downstream task)
- it produces s3://basic-datasets/output_2.txt (task outlets)

## DAG parameters (`params`, readable as `context["params"]` and `{{ params.x }}`)

none

## Schedule

- kind `manual`, cron `None`, interval seconds None, timezone `UTC`
- catchup False, start date 2024-08-01 00:00:00+00:00, max active runs 16
- triggered by assets: none (None)
- DAG settings: {"concurrency": 16, "fail_stop": false, "max_active_runs": 16, "max_active_tasks": 16, "max_consecutive_failed_dag_runs": 0, "render_template_as_native_obj": false}; DAG-level callbacks: False

## Graph

- edges (upstream -> downstream): none
- value flow (producer -> consumer): none
- assets in: none; assets out: s3://basic-datasets/output_2.txt
- connections (names): none; Variables (names): none
- default_args: `{}`

## Tasks

### `write_to_s3` (_PythonDecoratedOperator)

- upstream: none; trigger rule `all_success`; group None; owner airflow
- retries 0 (delay 300.0 s, exponential False); pool None; timeout None s; SLA None s; callbacks none
- reads a value from: nothing; pushes a value: True; outlets ['s3://basic-datasets/output_2.txt']; inlets none
- connections: none; Variables: none
- resolved arguments: `{"op_args": [], "op_kwargs": {}, "templates_dict": null}`
- arguments that hold a function: python_callable
- source of the function given as `python_callable` (`write_to_s3`):

```python
@task(outlets=[Dataset("s3://basic-datasets/output_2.txt")])
def write_to_s3():
    t_log.info("I'm pretending to write a txt file to S3.")
```
- callable source:

```python
@task(outlets=[Dataset("s3://basic-datasets/output_2.txt")])
def write_to_s3():
    t_log.info("I'm pretending to write a txt file to S3.")
```

## Behaviours that must be preserved

- the dependencies between tasks (what must finish before what), including trigger rules and which branch or skip decisions are taken
- the schedule (cron or interval) and its timezone, and what `catchup` and `start_date` meant for missed intervals
- retries, retry delay and backoff, pools and concurrency limits, execution timeouts
- every value that one task passes to another, and every output a task persists (outlets / assets, files, tables, keys)
- the failure behaviour of each step (what fails the run, what is skipped, what continues)
- the connections and Variables used, by name (values come from the environment, never from the code)
- the result of each external side effect (the file written, the message sent, the job started) for the same inputs

## Differences that are acceptable

- asset keys and the grouping of tasks into data products (one asset per data product is the target; several tasks may become one asset)
- per-task run records, durations and logs are replaced by per-asset runs and materializations
- XCom is replaced by asset values (IO manager) or by plain function calls inside an asset
- the Airflow UI, task instances and `dag_run` objects have no counterpart
- notification, wait and glue tasks may become sensors, hooks, run-status sensors or be folded into the producing asset
- operators may be replaced by an official integration or a registry component with the same effect

## What the converter did (reference draft, not a requirement)

- status: converted
- converter asset keys: s3/basic_datasets/output_2_txt
- tasks it stubbed (loud `AIRFLOW-CAVEAT`): write_to_s3
- stub caveat on `write_to_s3`: 't_log' (used by the callable) depends on Airflow

## Parity baseline

- none: no Airflow parity result was given (the DAG may be ineligible: external systems, long waits)
