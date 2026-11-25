# Specification: Airflow DAG `json_yaml_macros_toy`

Facts read from the IR (what Airflow built). Statements marked *inferred* are not facts. No secret value is in this file: credential-like arguments are redacted and connections and Variables are names only.

- file: `2-6-example-dags/json_yaml_macros_toy.py`; Airflow 2.10.5; tags: templating, toy; owners: airflow
- description: none

## What it does (from the facts)

- a 2-task DAG using _PythonDecoratedOperator (the task list)
- its final step(s) are print_json_macro, print_yaml_macro (tasks with no downstream task)
- it has no persisted output and uses no connection, so it may be a demonstration or a local computation (no outlets, no connection ids) *inferred*

## User-defined macros (names a template can use, e.g. `{{ my_json }}`)

values: `{"my_json": "{\"a\":\"hello\", \"b\":4, \"c\":true, \"d\": [ 1, 2, 3, 4, 5 ], \"e\" : {\"e1\" : 1, \"e2\" : \"bye\"}}", "my_yaml": "a: hello\nb: 4\nc: true\nd:\n  - 1\n  - 2\n  - 3\n  - 4\n  - 5\ne:\n  e1: 1\n  e2: bye"}`; functions (source not recorded): none

## DAG parameters (`params`, readable as `context["params"]` and `{{ params.x }}`)

none

## Schedule

- kind `manual`, cron `None`, interval seconds None, timezone `UTC`
- catchup False, start date 2023-04-18 00:00:00+00:00, max active runs 16
- triggered by assets: none (None)
- DAG settings: {"concurrency": 16, "fail_stop": false, "max_active_runs": 16, "max_active_tasks": 16, "max_consecutive_failed_dag_runs": 0, "render_template_as_native_obj": false}; DAG-level callbacks: False

## Graph

- edges (upstream -> downstream): none
- value flow (producer -> consumer): none
- assets in: none; assets out: none
- connections (names): none; Variables (names): none
- default_args: `{}`

## Tasks

### `print_json_macro` (_PythonDecoratedOperator)

- upstream: none; trigger rule `all_success`; group None; owner airflow
- retries 0 (delay 300.0 s, exponential False); pool None; timeout None s; SLA None s; callbacks none
- reads a value from: nothing; pushes a value: True; outlets none; inlets none
- connections: none; Variables: none
- resolved arguments: `{"op_args": [], "op_kwargs": {}, "templates_dict": {"my_config": "{{ macros.json.loads(my_json)}}"}}`
- arguments that hold a function: python_callable
- source of the function given as `python_callable` (`print_json_macro`):

```python
@task(templates_dict={"my_config": "{{ macros.json.loads(my_json)}}"})
def print_json_macro(**context):
    print(context["templates_dict"]["my_config"])
```
- callable source:

```python
@task(templates_dict={"my_config": "{{ macros.json.loads(my_json)}}"})
def print_json_macro(**context):
    print(context["templates_dict"]["my_config"])
```

### `print_yaml_macro` (_PythonDecoratedOperator)

- upstream: none; trigger rule `all_success`; group None; owner airflow
- retries 0 (delay 300.0 s, exponential False); pool None; timeout None s; SLA None s; callbacks none
- reads a value from: nothing; pushes a value: True; outlets none; inlets none
- connections: none; Variables: none
- resolved arguments: `{"op_args": [], "op_kwargs": {}, "templates_dict": {"my_config": "{{ macros.yaml.safe_load(my_yaml)}}"}}`
- arguments that hold a function: python_callable
- source of the function given as `python_callable` (`print_yaml_macro`):

```python
@task(templates_dict={"my_config": "{{ macros.yaml.safe_load(my_yaml)}}"})
def print_yaml_macro(**context):
    print(context["templates_dict"]["my_config"])
```
- callable source:

```python
@task(templates_dict={"my_config": "{{ macros.yaml.safe_load(my_yaml)}}"})
def print_yaml_macro(**context):
    print(context["templates_dict"]["my_config"])
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
- converter asset keys: json_yaml_macros_toy/print_json_macro, json_yaml_macros_toy/print_yaml_macro
- tasks it stubbed (loud `AIRFLOW-CAVEAT`): print_json_macro, print_yaml_macro
- stub caveat on `print_json_macro`: template uses macros ['json'], which the runtime does not provide
- stub caveat on `print_yaml_macro`: template uses macros ['yaml'], which the runtime does not provide

## Parity baseline

- none: no Airflow parity result was given (the DAG may be ineligible: external systems, long waits)
