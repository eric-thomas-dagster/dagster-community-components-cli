# Specification: Airflow DAG `partner_ingest`

Facts read from the IR (what Airflow built). Statements marked *inferred* are not facts. No secret value is in this file: credential-like arguments are redacted and connections and Variables are names only.

- file: `partner_ingest.py`; Airflow 2.10.5; tags: ingest, partner; owners: data-eng
- description: none

## What it does (from the facts)

- a 5-task DAG using BashOperator, HttpSensor, _PythonDecoratedOperator (the task list)
- its final step(s) are remove_tmp (tasks with no downstream task)
- it waits for an external condition before processing (a sensor task) *inferred*

## Schedule

- kind `cron_data_interval`, cron `0 */6 * * *`, interval seconds None, timezone `UTC`
- catchup False, start date 2024-01-01 00:00:00+00:00, max active runs 16
- triggered by assets: none (None)
- DAG settings: {"concurrency": 16, "fail_stop": false, "max_active_runs": 16, "max_active_tasks": 16, "max_consecutive_failed_dag_runs": 0, "render_template_as_native_obj": false}; DAG-level callbacks: False

## Graph

- edges (upstream -> downstream): fetch_export -> reshape, publish -> remove_tmp, reshape -> publish, wait_for_export -> fetch_export
- value flow (producer -> consumer): fetch_export -> reshape, reshape -> publish
- assets in: none; assets out: none
- connections (names): partner_api; Variables (names): none
- default_args: `{"owner": "data-eng", "retries": 2, "retry_delay": {"seconds": 600.0}}`

## Tasks

### `fetch_export` (_PythonDecoratedOperator)

- upstream: wait_for_export; trigger rule `all_success`; group None; owner data-eng
- retries 2 (delay 600.0 s, exponential False); pool None; timeout None s; SLA None s; callbacks none
- reads a value from: nothing; pushes a value: True; outlets none; inlets none
- connections: ['partner_api']; Variables: none
- resolved arguments: `{"op_args": [], "op_kwargs": {}, "templates_dict": null}`
- arguments that hold a function: python_callable
- source of the function given as `python_callable` (`fetch_export`):

```python
@task
def fetch_export() -> list[dict]:
    conn = BaseHook.get_connection("partner_api")
    resp = requests.get(f"{conn.host}/export/latest", headers={"Authorization": f"Bearer {conn.password}"}, timeout=60)
    resp.raise_for_status()
    return resp.json()["rows"]
```
- callable source:

```python
@task
def fetch_export() -> list[dict]:
    conn = BaseHook.get_connection("partner_api")
    resp = requests.get(f"{conn.host}/export/latest", headers={"Authorization": f"Bearer {conn.password}"}, timeout=60)
    resp.raise_for_status()
    return resp.json()["rows"]
```

### `publish` (_PythonDecoratedOperator)

- upstream: reshape; trigger rule `all_success`; group None; owner data-eng
- retries 2 (delay 600.0 s, exponential False); pool None; timeout None s; SLA None s; callbacks none
- reads a value from: reshape; pushes a value: True; outlets none; inlets none
- connections: none; Variables: none
- resolved arguments: `{"op_args": [{"key": "<redacted: a credential-like argument; its value is not recorded>", "xcom_from": "reshape"}], "op_kwargs": {}, "templates_dict": null}`
- arguments that hold a function: python_callable
- source of the function given as `python_callable` (`publish`):

```python
@task
def publish(summary: dict) -> str:
    line = f"partner export: {summary['customers']} customers, total {summary['total']}"
    print(line)
    return line
```
- callable source:

```python
@task
def publish(summary: dict) -> str:
    line = f"partner export: {summary['customers']} customers, total {summary['total']}"
    print(line)
    return line
```
- called with args `[{"xcom_from": "reshape", "key": "<redacted: a credential-like argument; its value is not recorded>"}]` kwargs `{}`

### `remove_tmp` (BashOperator)

- upstream: publish; trigger rule `all_success`; group None; owner data-eng
- retries 2 (delay 600.0 s, exponential False); pool None; timeout None s; SLA None s; callbacks none
- reads a value from: nothing; pushes a value: True; outlets none; inlets none
- connections: none; Variables: none
- resolved arguments: `{"append_env": false, "bash_command": "rm -rf /tmp/partner_export_tmp", "cwd": null, "env": null, "output_encoding": "utf-8", "skip_on_exit_code": [99]}`
- arguments that hold a function: output_processor

### `reshape` (_PythonDecoratedOperator)

- upstream: fetch_export; trigger rule `all_success`; group None; owner data-eng
- retries 2 (delay 600.0 s, exponential False); pool None; timeout None s; SLA None s; callbacks none
- reads a value from: fetch_export; pushes a value: True; outlets none; inlets none
- connections: none; Variables: none
- resolved arguments: `{"op_args": [{"key": "<redacted: a credential-like argument; its value is not recorded>", "xcom_from": "fetch_export"}], "op_kwargs": {}, "templates_dict": null}`
- arguments that hold a function: python_callable
- source of the function given as `python_callable` (`reshape`):

```python
@task
def reshape(rows: list[dict]) -> dict:
    """One record per customer, amounts summed, rows without an id dropped."""
    out: dict[str, float] = {}
    for r in rows:
        if r.get("customer_id"):
            out[r["customer_id"]] = out.get(r["customer_id"], 0.0) + float(r.get("amount", 0))
    return {"customers": len(out), "total": round(sum(out.values()), 2), "by_customer": out}
```
- callable source:

```python
@task
def reshape(rows: list[dict]) -> dict:
    """One record per customer, amounts summed, rows without an id dropped."""
    out: dict[str, float] = {}
    for r in rows:
        if r.get("customer_id"):
            out[r["customer_id"]] = out.get(r["customer_id"], 0.0) + float(r.get("amount", 0))
    return {"customers": len(out), "total": round(sum(out.values()), 2), "by_customer": out}
```
- called with args `[{"xcom_from": "fetch_export", "key": "<redacted: a credential-like argument; its value is not recorded>"}]` kwargs `{}`

### `wait_for_export` (HttpSensor)

- upstream: none; trigger rule `all_success`; group None; owner data-eng
- retries 2 (delay 600.0 s, exponential False); pool None; timeout None s; SLA None s; callbacks none
- reads a value from: nothing; pushes a value: True; outlets none; inlets none
- connections: ['partner_api']; Variables: none
- sensor: `{"exponential_backoff": false, "mode": "poke", "never_fail": false, "poke_interval": 120.0, "silent_fail": false, "soft_fail": false, "timeout": 21600.0}`
- resolved arguments: `{"deferrable": false, "endpoint": "export/status", "extra_options": {}, "headers": {}, "http_conn_id": "partner_api", "method": "GET", "request_kwargs": {}, "request_params": {}, "response_error_codes_allowlist": ["404"], "tcp_keep_alive": true, "tcp_keep_alive_count": 20, "tcp_keep_alive_idle": 120, "tcp_keep_alive_interval": 30}`
- arguments that hold a function: response_check
- source of the function given as `response_check` (`_export_ready`):

```python
def _export_ready(response) -> bool:
    return response.json().get("status") == "ready"
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
- converter asset keys: partner_ingest/wait_for_export, partner_ingest/remove_tmp, partner_ingest/fetch_export, partner_ingest/reshape, partner_ingest/publish
- tasks it stubbed (loud `AIRFLOW-CAVEAT`): fetch_export, wait_for_export
- stub caveat on `wait_for_export`: HttpSensor: response_check is a function (unusual_prefix_fe4965ea2b0dd5558582e422850ae884dbae0f18_partner_ingest._export_ready); the converter does not carry it into the step
- stub caveat on `fetch_export`: callable depends on an Airflow import (airflow.hooks.base)
- structural checks it generated: returned_value_present on partner_ingest/fetch_export, returned_value_present on partner_ingest/reshape

## Parity baseline

- none: no Airflow parity result was given (the DAG may be ineligible: external systems, long waits)
