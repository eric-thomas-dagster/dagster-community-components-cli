# Specification: Airflow DAG `toy_taskflow_bash`

Facts read from the IR (what Airflow built). Statements marked *inferred* are not facts. No secret value is in this file: credential-like arguments are redacted and connections and Variables are names only.

- file: `2-9-example-dags/toys/toy_taskflow_bash.py`; Airflow 2.10.5; tags: @task.bash, toy; owners: airflow
- description: none

## What it does (from the facts)

- a 2-task DAG using _BashDecoratedOperator, _PythonDecoratedOperator (the task list)
- its final step(s) are bash_task (tasks with no downstream task)
- it has no persisted output and uses no connection, so it may be a demonstration or a local computation (no outlets, no connection ids) *inferred*

## DAG parameters (`params`, readable as `context["params"]` and `{{ params.x }}`)

none

## Schedule

- kind `manual`, cron `None`, interval seconds None, timezone `UTC`
- catchup False, start date None, max active runs 16
- triggered by assets: none (None)
- DAG settings: {"concurrency": 16, "fail_stop": false, "max_active_runs": 16, "max_active_tasks": 16, "max_consecutive_failed_dag_runs": 0, "render_template_as_native_obj": false}; DAG-level callbacks: False

## Graph

- edges (upstream -> downstream): upstream_task -> bash_task
- value flow (producer -> consumer): upstream_task -> bash_task
- assets in: none; assets out: none
- connections (names): none; Variables (names): none
- default_args: `{}`

## Tasks

### `bash_task` (_BashDecoratedOperator)

- upstream: upstream_task; trigger rule `all_success`; group None; owner airflow
- retries 0 (delay 300.0 s, exponential False); pool None; timeout None s; SLA None s; callbacks none
- reads a value from: upstream_task; pushes a value: True; outlets none; inlets none
- connections: none; Variables: none
- resolved arguments: `{"bash_command": null, "cwd": null, "env": null, "op_args": [], "op_kwargs": {"dog_owner_data": {"key": "return_value", "xcom_from": "upstream_task"}}}`
- arguments that hold a function: python_callable
- source of the function given as `python_callable` (`bash_task`):

```python
@task.bash
def bash_task(dog_owner_data):
    names_of_dogless_people = []
    for name, dog in zip(dog_owner_data["names"], dog_owner_data["dogs"]):
        if dog < 1:
            names_of_dogless_people.append(name)

    if names_of_dogless_people:
        if len(names_of_dogless_people) == 1:
            return f'echo "{names_of_dogless_people[0]} urgently needs a dog!"'
        else:
            names_of_dogless_people_str = " and ".join(names_of_dogless_people)
            return f'echo "{names_of_dogless_people_str} urgently need a dog!"'
    else:
        return f'echo "All good, everyone has at least one dog!"'
```
- callable source:

```python
@task.bash
def bash_task(dog_owner_data):
    names_of_dogless_people = []
    for name, dog in zip(dog_owner_data["names"], dog_owner_data["dogs"]):
        if dog < 1:
            names_of_dogless_people.append(name)

    if names_of_dogless_people:
        if len(names_of_dogless_people) == 1:
            return f'echo "{names_of_dogless_people[0]} urgently needs a dog!"'
        else:
            names_of_dogless_people_str = " and ".join(names_of_dogless_people)
            return f'echo "{names_of_dogless_people_str} urgently need a dog!"'
    else:
        return f'echo "All good, everyone has at least one dog!"'
```
- called with args `[]` kwargs `{"dog_owner_data": {"xcom_from": "upstream_task", "key": "return_value"}}`

### `upstream_task` (_PythonDecoratedOperator)

- upstream: none; trigger rule `all_success`; group None; owner airflow
- retries 0 (delay 300.0 s, exponential False); pool None; timeout None s; SLA None s; callbacks none
- reads a value from: nothing; pushes a value: True; outlets none; inlets none
- connections: none; Variables: none
- resolved arguments: `{"op_args": [], "op_kwargs": {}, "templates_dict": null}`
- arguments that hold a function: python_callable
- source of the function given as `python_callable` (`upstream_task`):

```python
@task
def upstream_task():
    dog_owner_data = {
        "names": ["Trevor", "Grant", "Marcy", "Carly", "Philip"],
        "dogs": [1, 2, 2, 0, 4],
    }

    return dog_owner_data
```
- callable source:

```python
@task
def upstream_task():
    dog_owner_data = {
        "names": ["Trevor", "Grant", "Marcy", "Carly", "Philip"],
        "dogs": [1, 2, 2, 0, 4],
    }

    return dog_owner_data
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
- converter asset keys: toy_taskflow_bash/upstream_task, toy_taskflow_bash/bash_task
- tasks it stubbed (loud `AIRFLOW-CAVEAT`): none
- structural checks it generated: returned_value_present on toy_taskflow_bash/upstream_task

## Parity baseline

- Airflow ran it: success
- `bash_task`: success, value `'Carly urgently needs a dog!'`
- `upstream_task`: success, value `{'names': ['Trevor', 'Grant', 'Marcy', 'Carly', 'Philip'], 'dogs': [1, 2, 2, 0, 4]}`
- Airflow's own outcome from dag.test(); a value is a repr truncated to 120 characters and may contain data
