# Specification: Airflow DAG `literal_wrapper_example`

Facts read from the IR (what Airflow built). Statements marked *inferred* are not facts. No secret value is in this file: credential-like arguments are redacted and connections and Variables are names only.

- file: `2-8-example-dags/literal_wrapper_example.py`; Airflow 2.10.5; tags: 2-8, core, literal; owners: airflow
- description: none

## What it does (from the facts)

- a 2-task DAG using BashOperator (the task list)
- its final step(s) are echo_a_jinja_template, use_literal_wrapper_to_ignore_jinja_template (tasks with no downstream task)
- it has no persisted output and uses no connection, so it may be a demonstration or a local computation (no outlets, no connection ids) *inferred*

## DAG parameters (`params`, readable as `context["params"]` and `{{ params.x }}`)

`{"the_best_number": 231942}`

## Schedule

- kind `manual`, cron `None`, interval seconds None, timezone `UTC`
- catchup False, start date 2023-12-01 00:00:00+00:00, max active runs 16
- triggered by assets: none (None)
- DAG settings: {"concurrency": 16, "fail_stop": false, "max_active_runs": 16, "max_active_tasks": 16, "max_consecutive_failed_dag_runs": 0, "render_template_as_native_obj": false}; DAG-level callbacks: False

## Graph

- edges (upstream -> downstream): none
- value flow (producer -> consumer): none
- assets in: none; assets out: none
- connections (names): none; Variables (names): none
- default_args: `{}`

## Tasks

### `echo_a_jinja_template` (BashOperator)

- upstream: none; trigger rule `all_success`; group None; owner airflow
- retries 0 (delay 300.0 s, exponential False); pool None; timeout None s; SLA None s; callbacks none
- reads a value from: nothing; pushes a value: True; outlets none; inlets none
- connections: none; Variables: none
- resolved arguments: `{"append_env": false, "bash_command": "echo {{ params.the_best_number }}", "cwd": null, "env": null, "output_encoding": "utf-8", "skip_on_exit_code": [99]}`
- arguments that hold a function: output_processor

### `use_literal_wrapper_to_ignore_jinja_template` (BashOperator)

- upstream: none; trigger rule `all_success`; group None; owner airflow
- retries 0 (delay 300.0 s, exponential False); pool None; timeout None s; SLA None s; callbacks none
- reads a value from: nothing; pushes a value: True; outlets none; inlets none
- connections: none; Variables: none
- resolved arguments: `{"append_env": false, "bash_command": "{% raw %}echo {{ params.the_best_number }}{% endraw %}", "cwd": null, "env": null, "output_encoding": "utf-8", "skip_on_exit_code": [99]}`
- arguments that hold a function: output_processor

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
- converter asset keys: literal_wrapper_example/echo_a_jinja_template, literal_wrapper_example/use_literal_wrapper_to_ignore_jinja_template
- tasks it stubbed (loud `AIRFLOW-CAVEAT`): none

## Parity baseline

- Airflow ran it: failed
- `echo_a_jinja_template`: success, value `'231942'`
- `use_literal_wrapper_to_ignore_jinja_template`: failed, value `None`
- Airflow's own outcome from dag.test(); a value is a repr truncated to 120 characters and may contain data
