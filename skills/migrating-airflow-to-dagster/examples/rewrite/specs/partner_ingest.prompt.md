# Rewrite Airflow DAG `partner_ingest` as idiomatic Dagster

You are writing the Dagster code for one Airflow DAG from scratch. Do not transform the Airflow source line by line: understand what the DAG does from the specification below, then build what a Dagster
engineer would build for that behaviour. The converter's output is a draft and the fallback if your rewrite fails the gates; it is not the model to imitate.

## 1. Load the skills and follow them

1. Load the **`dagster-expert`** skill (by name) and follow its component hierarchy: components and registry/official integrations before hand-written code; create the project with
   `uvx create-dagster project rw_project --uv-sync` if it does not exist, never by hand; scaffold with `dg scaffold defs`; use its references for assets, schedules, sensors, automation and resources. Check every Dagster
   name and signature with it, `dg` (`dg list components --json`, `--help`) or the installed source. Do not invent an API.
2. Load the **`dagster-community-components`** skill (by name; for agents that load skills from files: `skills/dagster-community-components/SKILL.md` in this repository, with its `reference/cli.md`, `reference/catalog.md`,
   `reference/writing-yaml.md` and `reference/demo-workflow.md`). Follow its hard rules: never invent a component id, config field or CLI flag; search by capability and vendor before concluding that nothing exists;
   read the real `schema.json` fields before writing any `defs.yaml`; registry component first, official `dagster-<vendor>` integration second, hand-written code last.
3. Follow `reference/demo-workflow.md` of that skill for every stage of this DAG: **search** (`dagster-component search "<stage keywords>" --json`), **verify** that stages chain (`produces` / `consumes`; the component's README
   `Pairs with`), **read the schema** (`dagster-component info <id>`, `dagster-component schema <id> --format json`), **add and wire** (`dagster-component add <id>`, a `defs.yaml` using only fields you read), **validate**
   (`dg check defs`, `dg list defs --json`). `dagster-component list --available` lists the registry (plain `list` shows installed components). `dg list components --json` lists what is installed in the project.
4. For each registry component you choose, say in the header docstring which id you used, which schema fields you set, and its validation tier (`validation.level` in `dagster-component search --json`: `live`, `code`, `unit`,
   `component_only`, or none). A component that is not `live` carries a caveat sentence in the docstring. If you use an official integration or hand-written code instead, say why no registry component fit.

## 2. The specification (facts; statements marked inferred are not facts)

# Specification: Airflow DAG `partner_ingest`

Facts read from the IR (what Airflow built). Statements marked *inferred* are not facts. No secret value is in this file: credential-like arguments are redacted and connections and Variables are names only.

- file: `partner_ingest.py`; Airflow 2.10.5; tags: ingest, partner; owners: data-eng
- description: none

## What it does (from the facts)

- a 5-task DAG using BashOperator, HttpSensor, _PythonDecoratedOperator (the task list)
- its final step(s) are remove_tmp (tasks with no downstream task)
- it waits for an external condition before processing (a sensor task) *inferred*

## DAG parameters (`params`, readable as `context["params"]` and `{{ params.x }}`)

none

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
- resolved arguments: `{"op_args": [{"key": "return_value", "xcom_from": "reshape"}], "op_kwargs": {}, "templates_dict": null}`
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
- called with args `[{"xcom_from": "reshape", "key": "return_value"}]` kwargs `{}`

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
- resolved arguments: `{"op_args": [{"key": "return_value", "xcom_from": "fetch_export"}], "op_kwargs": {}, "templates_dict": null}`
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
- called with args `[{"xcom_from": "fetch_export", "key": "return_value"}]` kwargs `{}`

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


## 3. What to build: the target idioms

- **One asset per data product**, not per task. A data product is a task whose output is persisted (a table, file, S3 key, dbt model) or a returned value consumed by more than the next step. Tasks that only prepare or
  help produce one output are plain Python inside that asset.
- **Ops only where a restart point matters.** Use a graph-backed asset (`@dg.graph_asset` / `@dg.graph_multi_asset`; check the real API with `dagster-expert`) with ops when a step has retries, a long runtime or an expensive
  external call and deserves its own retry; otherwise keep it plain Python and say that a failed step restarts from the start of the asset.
- **A chain of dataframe or SQL transformations is ONE pipeline component asset** (`polars_pipeline`, `pandas_pipeline`, `warehouse_pipeline`, `pyspark_pipeline` in the registry) when the steps map to the component's
  ops: read its `schema.json` first and use only those ops. If the spec's code does something the ops cannot express, keep that step as Python and say so.
- **Notification, wait and cleanup tasks are not assets.** Email, Slack and other notifications become a run-status sensor, a hook or part of the producing asset; sensors-as-waits become Dagster sensors or an automation
  condition; `EmptyOperator` disappears. The header must say which for each.
- Dagster's own model for the rest: schedules with the spec's cron and timezone (`dg.ScheduleDefinition`), retry policies and pools from the spec, `AutomationCondition` for asset-triggered DAGs, dbt through `DbtProjectComponent`
  for Cosmos, connections as resources or environment variables **by name** (never a value), Variables as environment variables.
- Add **non-blocking structural asset checks** only where the source shows something checkable (a consumed value is not None, an S3 object written exists). They are structural, not business, checks: do not invent business rules.
- Preserve every behaviour under "must be preserved" in the spec; the "acceptable differences" are yours to use.

## 4. Where to write it

- The module `rw_pkg.defs.partner_ingest` (`src/rw_pkg/defs/partner_ingest.py`), plus any component folders under `src/rw_pkg/defs/`. Define a job named `partner_ingest` over the DAG's assets and the schedule from the spec.
- The first lines of the module are a docstring that **accounts for every Airflow task**: one line per task id in the form `<task_id>: replaced by asset `<asset key>`` / `folded into `<asset key>`` /
  `dropped: <reason>` / `merged into ...`, followed by the registry components and tiers you used and any behaviour you could not preserve (stated, not hidden).
- Secrets: environment variables and resources only. No password, token, key or URL with credentials in the code or YAML.

## 5. The acceptance gates

Your rewrite is accepted only when `python3 scripts/accept_rewrite.py --spec rewrite/partner_ingest.spec.json --module rw_pkg.defs.partner_ingest --project <project root> [--defs-dir <component folder>] [--parity <Airflow results>] --manifest manifest.json`
passes every gate:

1. `dg check defs` passes (or, where `dg` is not installed, the definitions load; the gate says which);
2. the generated pytest passes: the module loads, a job named `partner_ingest` exists, the schedule's cron and timezone equal Airflow's, and for a DAG without external systems the DAG materializes and every asset check passes;
3. asset checks are present where the source supports them (at least 2 here);
4. parity: where an Airflow baseline exists, the same overall outcome and the same value as Airflow for every task the header says is `replaced by` an asset (a task `folded into` another has no value of its own to compare);
5. no secret values;
6. no invented APIs: every import and every component `type:` resolves;
7. every Airflow task is accounted for in the header docstring.

A rewrite that fails a gate stays deferred, with the failure recorded; fix it and run the gates again. Never mark a unit complete yourself.

## 6. The converter's output (reference and fallback)

The converter's output for this DAG is at `converter_output/partner_ingest.py` (status `converted`; stubbed tasks: fetch_export, wait_for_export). Read it as a draft: it shows the source's behaviour translated one task at a time. Do not copy its structure; use it to check that you have not lost behaviour.

If the rewrite cannot pass the gates, the converter's output is what ships (or the DAG stays deferred where the converter deferred it). Say plainly in your final message what you could not preserve or verify.

## 7. Cost

This route spends model effort on every DAG (reading the spec, searching the registry, writing and iterating until the gates pass). Use it for the DAGs where idiomatic output or wider operator coverage matters; the
converter stays the default for mechanical DAGs.
