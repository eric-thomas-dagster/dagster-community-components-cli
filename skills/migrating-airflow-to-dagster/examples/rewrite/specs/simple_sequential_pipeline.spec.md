# Specification: Airflow DAG `simple_sequential_pipeline`

Facts read from the IR (what Airflow built). Statements marked *inferred* are not facts. No secret value is in this file: credential-like arguments are redacted and connections and Variables are names only.

- file: `simple_sequential.py`; Airflow 2.10.5; tags: example, sequential; owners: airflow
- description: none

## What it does (from the facts)

- a 3-task DAG using _PythonDecoratedOperator (the task list)
- its final step(s) are save_results (tasks with no downstream task)
- it has no persisted output and uses no connection, so it may be a demonstration or a local computation (no outlets, no connection ids) *inferred*

## DAG parameters (`params`, readable as `context["params"]` and `{{ params.x }}`)

none

## Schedule

- kind `cron_data_interval`, cron `0 9 * * *`, interval seconds None, timezone `UTC`
- catchup False, start date 2024-01-01 00:00:00+00:00, max active runs 16
- triggered by assets: none (None)
- DAG settings: {"concurrency": 16, "fail_stop": false, "max_active_runs": 16, "max_active_tasks": 16, "max_consecutive_failed_dag_runs": 0, "render_template_as_native_obj": false}; DAG-level callbacks: False

## Graph

- edges (upstream -> downstream): fetch_data -> process_data, process_data -> save_results
- value flow (producer -> consumer): fetch_data -> process_data, process_data -> save_results
- assets in: none; assets out: none
- connections (names): none; Variables (names): none
- default_args: `{"retries": 3, "retry_delay": {"seconds": 300.0}}`

## Tasks

### `fetch_data` (_PythonDecoratedOperator)

- upstream: none; trigger rule `all_success`; group None; owner airflow
- retries 3 (delay 300.0 s, exponential False); pool None; timeout None s; SLA None s; callbacks none
- reads a value from: nothing; pushes a value: True; outlets none; inlets none
- connections: none; Variables: none
- resolved arguments: `{"op_args": [], "op_kwargs": {}, "templates_dict": null}`
- arguments that hold a function: python_callable
- source of the function given as `python_callable` (`fetch_data`):

```python
@task
def fetch_data(source: str = "api") -> dict:
    """Fetch data from a source."""
    print(f"Fetching data from {source}")
    return {"source": source, "data": [1, 2, 3, 4, 5]}
```
- callable source:

```python
@task
def fetch_data(source: str = "api") -> dict:
    """Fetch data from a source."""
    print(f"Fetching data from {source}")
    return {"source": source, "data": [1, 2, 3, 4, 5]}
```

### `process_data` (_PythonDecoratedOperator)

- upstream: fetch_data; trigger rule `all_success`; group None; owner airflow
- retries 3 (delay 300.0 s, exponential False); pool None; timeout None s; SLA None s; callbacks none
- reads a value from: fetch_data; pushes a value: True; outlets none; inlets none
- connections: none; Variables: none
- resolved arguments: `{"op_args": [{"key": "return_value", "xcom_from": "fetch_data"}], "op_kwargs": {}, "templates_dict": null}`
- arguments that hold a function: python_callable
- source of the function given as `python_callable` (`process_data`):

```python
@task
def process_data(data: dict) -> dict:
    """Process the fetched data."""
    print(f"Processing data from {data['source']}")
    processed = {
        "source": data["source"],
        "processed": [x * 2 for x in data["data"]]
    }
    return processed
```
- callable source:

```python
@task
def process_data(data: dict) -> dict:
    """Process the fetched data."""
    print(f"Processing data from {data['source']}")
    processed = {
        "source": data["source"],
        "processed": [x * 2 for x in data["data"]]
    }
    return processed
```
- called with args `[{"xcom_from": "fetch_data", "key": "return_value"}]` kwargs `{}`

### `save_results` (_PythonDecoratedOperator)

- upstream: process_data; trigger rule `all_success`; group None; owner airflow
- retries 3 (delay 300.0 s, exponential False); pool None; timeout None s; SLA None s; callbacks none
- reads a value from: process_data; pushes a value: True; outlets none; inlets none
- connections: none; Variables: none
- resolved arguments: `{"op_args": [{"key": "return_value", "xcom_from": "process_data"}], "op_kwargs": {}, "templates_dict": null}`
- arguments that hold a function: python_callable
- source of the function given as `python_callable` (`save_results`):

```python
@task
def save_results(processed: dict) -> str:
    """Save the processed results."""
    result = f"Saved {len(processed['processed'])} items from {processed['source']}"
    print(result)
    return result
```
- callable source:

```python
@task
def save_results(processed: dict) -> str:
    """Save the processed results."""
    result = f"Saved {len(processed['processed'])} items from {processed['source']}"
    print(result)
    return result
```
- called with args `[{"xcom_from": "process_data", "key": "return_value"}]` kwargs `{}`

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
- converter asset keys: simple_sequential_pipeline/fetch_data, simple_sequential_pipeline/process_data, simple_sequential_pipeline/save_results
- tasks it stubbed (loud `AIRFLOW-CAVEAT`): none
- structural checks it generated: returned_value_present on simple_sequential_pipeline/fetch_data, returned_value_present on simple_sequential_pipeline/process_data

## Parity baseline

- Airflow ran it: success
- `fetch_data`: success, value `{'source': 'api', 'data': [1, 2, 3, 4, 5]}`
- `process_data`: success, value `{'source': 'api', 'processed': [2, 4, 6, 8, 10]}`
- `save_results`: success, value `'Saved 5 items from api'`
- Airflow's own outcome from dag.test(); a value is a repr truncated to 120 characters and may contain data
