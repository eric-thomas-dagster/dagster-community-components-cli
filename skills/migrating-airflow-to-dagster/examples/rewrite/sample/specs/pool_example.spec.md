# Specification: Airflow DAG `pool_example`

Facts read from the IR (what Airflow built). Statements marked *inferred* are not facts. No secret value is in this file: credential-like arguments are redacted and connections and Variables are names only.

- file: `2-8-example-dags/pool_example.py`; Airflow 2.10.5; tags: 2-8, Pools, core; owners: airflow
- description: none

## What it does (from the facts)

- a 2-task DAG using _PythonDecoratedOperator (the task list)
- its final step(s) are jump_into_the_pool (tasks with no downstream task)
- it has no persisted output and uses no connection, so it may be a demonstration or a local computation (no outlets, no connection ids) *inferred*

## DAG parameters (`params`, readable as `context["params"]` and `{{ params.x }}`)

`{"num_guests": 23}`

## Schedule

- kind `manual`, cron `None`, interval seconds None, timezone `UTC`
- catchup False, start date 2023-12-01 00:00:00+00:00, max active runs 16
- triggered by assets: none (None)
- DAG settings: {"concurrency": 16, "fail_stop": false, "max_active_runs": 16, "max_active_tasks": 16, "max_consecutive_failed_dag_runs": 0, "render_template_as_native_obj": false}; DAG-level callbacks: False

## Graph

- edges (upstream -> downstream): count_guests -> jump_into_the_pool
- value flow (producer -> consumer): count_guests -> jump_into_the_pool
- assets in: none; assets out: none
- connections (names): none; Variables (names): none
- default_args: `{}`

## Tasks

### `count_guests` (_PythonDecoratedOperator)

- upstream: none; trigger rule `all_success`; group None; owner airflow
- retries 0 (delay 300.0 s, exponential False); pool None; timeout None s; SLA None s; callbacks none
- reads a value from: nothing; pushes a value: True; outlets none; inlets none
- connections: none; Variables: none
- resolved arguments: `{"op_args": [], "op_kwargs": {}, "templates_dict": null}`
- arguments that hold a function: python_callable
- source of the function given as `python_callable` (`count_guests`):

```python
@task
def count_guests(**context):
    num_guests = context["params"]["num_guests"]
    print(f"There are {num_guests} people at my garden party!")
    print("They all want to jump into the pool!")
    return [i for i in range(num_guests)]
```
- callable source:

```python
@task
def count_guests(**context):
    num_guests = context["params"]["num_guests"]
    print(f"There are {num_guests} people at my garden party!")
    print("They all want to jump into the pool!")
    return [i for i in range(num_guests)]
```

### `jump_into_the_pool` (_PythonDecoratedOperator)

- upstream: count_guests; trigger rule `all_success`; group None; owner airflow
- retries 0 (delay 300.0 s, exponential False); pool my_garden_pool (its slot count lives in Airflow's metadata database and is not in the IR); timeout None s; SLA None s; callbacks ['post_execute', 'pre_execute']
- reads a value from: count_guests; pushes a value: True; outlets none; inlets none
- connections: none; Variables: none
- dynamic mapping: `{"args": {"guest": {"key": "return_value", "kind": "xcom", "task_id": "count_guests", "wrapper": "PlainXComArg"}}, "fixed": {}, "fixed_unserializable": [], "form": "expand", "op_args": false, "partial": {}}`
- resolved arguments: `{"operator_extra_links": [], "operator_name": "@task", "start_from_trigger": false, "start_trigger_args": null, "subdag": null, "supports_lineage": false, "task_type": "_PythonDecoratedOperator", "template_ext": [], "template_fields": ["templates_dict", "op_args", "op_kwargs"], "template_fields_renderers": {"op_args": "py", "op_kwargs": "py", "templates_dict": "json"}, "ui_color": "#ffefeb", "ui_fgcolor": "#000"}`
- arguments that hold a function: operator_class, python_callable
- source of the function given as `python_callable` (`jump_into_the_pool`):

```python
@task(
    pool=MY_POOL_NAME,
)
def jump_into_the_pool(guest):
    print(f"Guest number {guest} is jumping into the pool!")
    print(f"Guest number {guest} is swimming in the pool!")
    swim_time = 10
    time.sleep(swim_time)
    print(
        f"After {swim_time} seconds, guest number {guest} is getting out of the pool!"
    )
```
- callable source:

```python
@task(
    pool=MY_POOL_NAME,
)
def jump_into_the_pool(guest):
    print(f"Guest number {guest} is jumping into the pool!")
    print(f"Guest number {guest} is swimming in the pool!")
    swim_time = 10
    time.sleep(swim_time)
    print(
        f"After {swim_time} seconds, guest number {guest} is getting out of the pool!"
    )
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
- converter asset keys: pool_example/count_guests, pool_example/jump_into_the_pool
- tasks it stubbed (loud `AIRFLOW-CAVEAT`): none
- note: dynamic task mapping became dynamic outputs inside one graph asset: the whole DAG runs as one Dagster run, mapped tasks are mapped ops and a task after them receives the collected list. Differences from Airflow: retries and pools apply per op; individual tasks cannot be re-run on their own (the asset is the unit), and an empty list skips the mapped step and gives the next step an empty list.

## Parity baseline

- Airflow ran it: success
- `count_guests`: success, value `[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22]`
- `jump_into_the_pool`: success, value `None`
- Airflow's own outcome from dag.test(); a value is a repr truncated to 120 characters and may contain data
