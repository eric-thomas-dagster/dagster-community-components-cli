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

- the dependencies between tasks (what must finish before what), including trigger rules, which branch or skip decisions are taken, and the effects of the branch taken and none of the branch not taken
- each task's end state in Airflow (success, failed, skipped, upstream_failed): `skipped` is kept, or its loss is declared in the rewrite's decision record
- the schedule (cron or interval) and its timezone, and what `catchup` and `start_date` meant for missed intervals
- retries, retry delay and backoff, pools and concurrency limits, execution timeouts
- every value that one task passes to another, and every output a task persists (outlets / assets, files, tables, keys)
- the failure behaviour of each step (what fails the run, what is skipped, what continues)
- the connections and Variables used, by name (values come from the environment, never from the code)
- the result of each external side effect (the file written, the message sent, the job started) for the same inputs

## Differences that are acceptable

- asset keys and the grouping of tasks into data products (one asset per data product is the target; several tasks may become one asset)
- per-task run records, durations and logs are replaced by per-asset runs and materializations (a task's `skipped` state is NOT in this list: it is kept, or its loss is declared in the decision record)
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

**The principle: native Dagster forms first; plain Python inside the asset is the stated fallback.** For branching, short circuits, skips, rejoins and runtime fan-out Dagster has native forms, verified by running
on the installed version and shown in the patterns below when the spec shows the construct: a ShortCircuit is an optional-output asset or op; a branch to different downstream assets/ops is a `@multi_asset` with
`AssetOut(is_required=False)` or an op with `Out(is_required=False)` (steps below an unyielded output are skipped, the run succeeds); a branch then rejoin to one product is a `@graph_asset` whose join op takes a
fan-in `list`; a runtime list is `DynamicOut` + `.map()`. Use them. Plain Python inside one asset (`if/else`, early `return`, `for`, `try/finally`, local variables for XCom) is the FALLBACK, for only three cases:
(a) the branches produce the same output and differ only in logic, (b) the native form is not verified to work for this case, (c) it failed a gate; the decision record then says `fallback to plain Python:
<reason>`. The product-granularity principle stays (one asset per data product, not one per task: tasks that only prepare a single output fold into its asset) but it is not a reason to fold a real branch into plain
Python. Dagster fixes a run's shape before it starts, so a runtime decision is an optional output or code inside an asset, never a different set of assets chosen at run time (`can_subset` is launch-time selection;
do not add it; verified unnecessary). Folding into plain Python loses the skipped state, per-step retries, per-step visibility and partial restart: say so in the decision record.

- **One asset per data product**, not per task. A data product is a task whose output is persisted (a table, file, S3 key, dbt model) or a returned value consumed by more than the next step. Tasks that only prepare or
  help produce one output are plain Python inside that asset.
- **Ops only where a restart point matters.** Use a graph-backed asset (`@dg.graph_asset` / `@dg.graph_multi_asset`; check the real API with `dagster-expert`) with ops when a step has retries, a long runtime or an expensive
  external call and deserves its own retry; otherwise keep it plain Python and say that a failed step restarts from the start of the asset.
- **A chain of dataframe or SQL transformations is ONE pipeline component asset** (`polars_pipeline`, `pandas_pipeline`, `warehouse_pipeline`, `pyspark_pipeline` in the registry) when the steps map to the component's
  ops: read its `schema.json` first and use only those ops. If the spec's code does something the ops cannot express, keep that step as Python and say so.
- **Notification, wait and cleanup tasks are not assets.** Email, Slack and other notifications become a run-status sensor, a hook or part of the producing asset; sensors-as-waits become Dagster sensors or an automation
  condition; `EmptyOperator` disappears. The header must say which for each.
- Dagster's own model for the rest: schedules with the spec's cron and timezone (`dg.ScheduleDefinition`), retry policies from the spec, pools as the first-class `pool=` on the asset or op (the limit is instance configuration: `dagster instance concurrency set <pool> <n>`; say who sets it), `AutomationCondition` for asset-triggered DAGs, dbt through `DbtProjectComponent`
  for Cosmos, connections as resources or environment variables **by name** (never a value), Variables as environment variables.
- A task that needs its own Python environment (`PythonVirtualenvOperator`, `ExternalPythonOperator`, `@task.virtualenv`): declare the requirements and run it through Dagster Pipes (`PipesSubprocessClient` with that environment's interpreter), with a loud failure when the interpreter is missing; never silently run it in the current interpreter (the venv pattern is injected when the spec shows one).
- Add **non-blocking structural asset checks** only where the source shows something checkable (a consumed value is not None, an S3 object written exists). They are structural, not business, checks: do not invent business rules.
- Preserve every behaviour under "must be preserved" in the spec. That includes the skip and branch decisions and what each task's end state was in Airflow (`success`, `failed`, `skipped`, `upstream_failed`): a task's
  `skipped` state is kept (its own asset with `airflow/state` metadata, or a skippable output: see the patterns) or its loss is declared in the decision record; it is never silently dropped. A branch task that is not taken and is folded into the branching asset is accounted for as `folded into `<asset>`: branch not taken` and declared under `skip/branch`. Of the "acceptable differences", use only
  those that name something Dagster cannot express.

### Patterns this DAG needs (verified Dagster code)

The spec shows the constructs below. Each pattern shows the NATIVE Dagster form first and the plain-Python-inside-one-asset form as the stated fallback; the code was executed on the installed Dagster and its skip/state behaviour asserted. Use the native form, change the names, keep the semantics; fall back to plain Python only for the reasons the pattern lists, and then say `fallback to plain Python: <reason>` in the decision record. The representation table says what Dagster cannot express: say so in the header and the decision record instead of hiding it.

## What Dagster can and cannot express, per Airflow state and scenario

**Choice rule (native first, plain Python as the fallback):**
- ShortCircuit -> an **optional-output asset or op** (a generator that returns before yielding; `short_circuit.py`);
- branch to different downstream assets, ops, retries, resources or checks -> **`@multi_asset` with `AssetOut(is_required=False)`** or an op with `Out(is_required=False)` (`branching.py`, (a));
- branch then rejoin into one product -> **`@graph_asset` with an op using `Out(is_required=False)` per branch and a fan-in `list` input** (`branching.py`, (b));
- a list known only at run time -> **`DynamicOut` + `.map()`** (`mapped_tasks.py`);
- **fallback, plain Python in the asset** (`branching_plain_fallback.py`, an early `return`, a `for` loop): only when (a) the branches produce the same output and differ only in logic, (b) the native form is not
  verified to work for this case, or (c) it failed a gate. State it: `- skip/branch: fallback to plain Python: <reason>` in the decision record.
Do not add `can_subset=True`: launch-time selection, verified unnecessary and harmless here, not a runtime decision.

| Airflow state / scenario | Form that reproduces it | Same-run behaviour? |
|---|---|---|
| `success` | the asset materializes | exact |
| `failed` | the step fails; the run fails | exact |
| `up_for_retry` | `RetryPolicy` on the asset or op | policy exact; no per-task retry history |
| `upstream_failed` | a downstream step of a failed upstream asset is not run | exact for separate assets; folded tasks fail with their step |
| branch not taken, its own asset (`multi_asset` optional outs, graph_asset ops): the NATIVE form | output not yielded: asset not materialized, every step below it SKIPPED, run succeeds | **same run, skipped state expressed** (verified) |
| branch not taken, folded into the branching asset (plain Python): the FALLBACK | not executed, no state of its own | **state not expressible**; the effects are exact. Header: `folded into `<asset>`: branch not taken`; record: `fallback to plain Python: <reason>` |
| join after branches, `all_success` | a regular downstream asset with inputs from several optional outputs | exact: skipped if ANY input is skipped |
| join after branches, `none_failed_min_one_success` / `one_success`, same run | `graph_asset` with a fan-in `list` input over the branch ops (`rejoined`) | **same run, runs with whichever branch ran** (verified); with none taken it is skipped, as in Airflow |
| same join as separate top-level assets | `AutomationCondition.any_deps_updated()` on the join | approximation: requested in a LATER run after any one branch ran, not the same run (verified with `evaluate_automation_conditions`) |
| join as a regular asset with two inputs when you wanted min_one_success | not reproducible: it is skipped when either branch is | mismatch with Airflow (verified); use the fan-in form |
| ShortCircuit task itself | success: the condition part runs | exact |
| ShortCircuit downstream skipped (plain Python), the FALLBACK | early `return`; guarded tasks folded | effects exact, state not expressed |
| ShortCircuit downstream skipped (optional output), NATIVE | generator that returns before yielding: not materialized, everything below skipped | **same run, skipped expressed** (verified). A bare `return` with no `yield` does NOT skip (the None counts as the output): claim checked and refuted |
| a task that skips itself and is its own asset, no optional output wanted | metadata `airflow/state: skipped` and the SKIPPED sentinel (`skip.py`) | closest: shows as materialized, the metadata carries the state |
| mapped task with 0 instances (Airflow: skipped, downstream skipped) | `DynamicOut` yielding nothing: `.collect()` gets `[]` and the step below RUNS with `[]` (verified); a plain loop does the same. To reproduce the skip, give the collecting step an optional output and return before yielding when the list is empty | closest by default |
| `all_done` after a failure | `try/finally` in one asset | closest: cleanup runs, then the step is failed (Airflow: all_done task succeeds) |
| `one_failed` / `all_failed` | run-failure sensor launching a handler job | approximation: after the run |
| `deferred`, `queued`, `scheduled`, `running`, `removed` | none | not expressible |
| sensor `reschedule` | a Dagster sensor that requests the run | closest |
| `depends_on_past` waiting | the asset fails when its previous step failed | closest (waits vs fails) |
| `all_done` cleanup, native | `@dg.success_hook` + `@dg.failure_hook` on the asset or graph_asset (verified to fire on both): the asset keeps its true state, the cleanup runs after it either way | closest: the cleanup is not a task with its own `success` state |
| mapped task, one failed instance | `DynamicOut` + `.map()`: that step fails, the run fails, the other items ran, the step after `.collect()` is not run (verified) | same as Airflow (instance failed, others success, downstream upstream_failed) |
| mapped items as tracked units | `DynamicPartitionsDefinition` (keys added by a sensor or code), one partitioned asset | closest: a partition status per item, not a task instance |
| pool `pool="x"` | first-class `pool="x"` on asset/op (not graph_asset); limit via `dagster instance concurrency set x <n>` (instance config) | slot count is not in the spec; per-run limits and `pool_slots` > 1 not expressible |
| long wait / sensor `reschedule` / async job | a `@sensor` emitting `RunRequest` (`condition_sensor`, `job_done_sensor`), or Pipes | closest: the wait is outside every run, no timeout state |
| short wait, same run needed | `poll_until` in the asset (fallback) | holds the worker like `mode='poke'` |
| TriggerDagRun / ExternalTaskSensor / asset trigger | asset dependency + `AutomationCondition.eager()` / `on_cron`; with conf: `run_status_sensor` yielding `RunRequest(run_config=...)` (verified eager requests the downstream) | separate run: `wait_for_completion` not reproduced |
| XCom | return value as an asset argument; observability values as `add_output_metadata` | exact for small values |
| TaskGroup | `group_name` + `key_prefix` (consumers name the prefixed key via `AssetIn(key=...)`); a factory for a template group | exact except the UI grouping |

## What folding loses (state it in the decision record)

Per-step retries (one `RetryPolicy` for the whole asset), per-step visibility in the UI (one run step, not one per task), partial restart (a failed asset restarts from its start). If any of these
matters, the alternative is a graph asset (`@dg.graph_asset`) with one op per step and a `RetryPolicy` on each op; say so in the record.



# Sensors with timeout, soft_fail, mode

Airflow: poke every `poke_interval` until true; after `timeout` the sensor FAILS, or is SKIPPED with `soft_fail=True`; `mode='reschedule'` frees the worker between pokes.

**Native (anything but a short wait): never `time.sleep` in an asset.** The wait is a Dagster sensor that evaluates the condition and emits `RunRequest` (`condition_sensor` in `sensors.py`). For an async external
job in the middle of a pipeline: asset A submits and returns the job id, a sensor polls completion and requests the run holding asset B (`submit_job`, `job_done_sensor`, `after_job`); Dagster Pipes is the other
native route for a job that reports back while it runs. The wait is then outside every run: there is no timeout state, `reschedule` is the closest of Airflow's modes, and it is said so in the decision record
(`sensor modes`). Reuse the shape the converter already emits for Airflow sensors.

**Fallback (short waits whose follow-on work must be in the same run): `poll_until(check, timeout, interval)` inside the asset** (`wait_then_work`; `<sensor>: folded into `<asset>`: poke loop`). On timeout raise
`dg.Failure` (Airflow: failed); with `soft_fail` record `airflow/state: skipped` and return the SKIPPED sentinel (`skip.py`). It holds the worker like `mode='poke'`; record `fallback to plain Python: <reason>`.

Tested: the sensors return `SkipReason` / `RunRequest` correctly, the poll loop (fake clock), timeout failure, soft_fail skip, and that the sensor returns `SkipReason` / `RunRequest` correctly. Not run against a real external condition.

```python  # sensors.py (tested)
"""Pattern: sensors with timeout / soft_fail / mode='reschedule'.

Airflow sensor: pokes every `poke_interval` until the condition is true; after `timeout` it FAILS (AirflowSensorTimeout), or is marked SKIPPED when `soft_fail=True`. mode='reschedule' frees the
worker between pokes; mode='poke' holds it.
NATIVE for anything but a short wait: do not `time.sleep` in an asset; the wait is a Dagster sensor that evaluates the condition and emits `RunRequest` (`condition_sensor` below), and for an async external job
mid-pipeline: asset A submits and returns the job id, a sensor polls completion and requests the run that holds asset B (`submit_job` / `job_done_sensor` below; Dagster Pipes is the other native route for jobs
that report back while running). FALLBACK, when the wait is short and the work after it must be in the same run: `poll_until` inside the asset that needs the condition. soft_fail -> the asset records `airflow/state: skipped` and returns SKIPPED (see skip.py)
so consumers skip; otherwise it raises `dg.Failure`. This HOLDS the worker like mode='poke'.
Dagster, reschedule-like (long waits): a Dagster sensor evaluates the condition each tick and requests the run only when true (`condition_sensor`); the wait is no longer part of any run and
there is no timeout state: say that in the header. `time.sleep` and the clock are parameters so the logic is testable.
"""
import time
from typing import Callable

import dagster as dg

SKIPPED = "__airflow_skipped__"


def poll_until(check: Callable[[], bool], timeout: float, interval: float, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> bool:
    """True when `check()` became true, False when `timeout` seconds passed first."""
    start = clock()
    while True:
        if check():
            return True
        if clock() - start + interval > timeout:
            return False
        sleep(interval)


READY = {"ok": False}


@dg.asset
def wait_then_work(context: dg.AssetExecutionContext) -> dg.Output:
    soft_fail = False
    if not poll_until(lambda: READY["ok"], timeout=1.0, interval=0.1):
        if soft_fail:
            return dg.Output(SKIPPED, metadata={"airflow/state": "skipped", "reason": "sensor timed out (soft_fail)"})
        raise dg.Failure("sensor timed out")                            # AirflowSensorTimeout
    return dg.Output("worked", metadata={"airflow/state": "success"})


@dg.job
def downstream_job():
    @dg.op
    def go():
        return 1
    go()


@dg.sensor(job=downstream_job, minimum_interval_seconds=60)
def condition_sensor(context: dg.SensorEvaluationContext):
    if READY["ok"]:                                                      # the poke: replace with the real condition
        return dg.RunRequest(run_key="ready")
    return dg.SkipReason("condition not met yet")


JOBS = {"job-1": "running"}                                              # the external system's state (a stand-in)


@dg.asset
def submit_job() -> str:
    return "job-1"                                                       # asset A: submit the external job and return its id


@dg.asset
def after_job() -> str:
    return "asset B ran after the external job completed"


after_job_run = dg.define_asset_job("after_job_run", selection=[after_job])


@dg.sensor(job=after_job_run, minimum_interval_seconds=30)
def job_done_sensor(context: dg.SensorEvaluationContext):
    done = [j for j, st in JOBS.items() if st == "done"]
    if not done:
        return dg.SkipReason("external job still running")
    return dg.RunRequest(run_key=done[0])                                # one run per completed job (run_key dedupes)


defs = dg.Definitions(assets=[wait_then_work, submit_job, after_job], jobs=[downstream_job, after_job_run], sensors=[condition_sensor, job_done_sensor])
```

# XCom

A small value the next asset needs: return it and take it as an ordinary asset argument (`xcom.py`). A value that is only for observability: `context.add_output_metadata` / `Output(metadata=...)`: shown in
the UI, not passed on. Tasks folded into one asset use local variables. Keep a value out of metadata if it is large or sensitive. XCom pulled by `key` other than `return_value` or by `task_ids` of several
tasks: return a dict keyed by those names. Decision record has no XCom line unless a pull cannot be mapped (then `not preserved: <why>`).

```python  # xcom.py (tested)
"""Pattern: XCom. A small value the NEXT asset needs: return it, and take it as an ordinary asset argument (the IO manager is the XCom). A value that is only for observability: attach it as metadata
(`dg.Output(value, metadata=...)`, `dg.MaterializeResult(metadata=...)` or `context.add_output_metadata`), which is shown in the UI and is not passed on. Tasks folded into one asset use local variables."""
import dagster as dg


@dg.asset
def producer() -> dict:
    return {"rows": 3}                                                  # xcom_push / return value consumed by the next asset


@dg.asset
def consumer(producer: dict) -> int:
    return producer["rows"] * 2                                         # xcom_pull -> an argument


@dg.asset
def observed_only(context: dg.AssetExecutionContext) -> None:
    context.add_output_metadata({"row_count": 3, "source": "api"})      # a value nobody consumes: metadata, not a return value


@dg.asset
def with_output_metadata() -> dg.Output:
    return dg.Output(7, metadata={"note": "also passed to consumers"})


defs = dg.Definitions(assets=[producer, consumer, observed_only, with_output_metadata])
```

# Templates, params, Variables, connections

Use the converter's translation set (ds, ds_nodash, ts, logical_date, data_interval_start/end, ds_add, ds_format) and `templating_config.py` (all of it executed): `{{ ds }}` / `data_interval_*` -> `partition_key` /
`partition_time_window` of a time-partitioned asset (add a `DailyPartitionsDefinition` when the interval matters; an unpartitioned asset has only the run time, which is NOT the data interval: say so);
`{{ params.x }}` -> a `dg.Config` field; `{{ var.value.x }}` -> an environment variable by name (`EnvVar`); connections -> a `ConfigurableResource` with `EnvVar` fields injected as an argument, mockable in tests.
Header: `<task>: replaced by asset `<key>`` plus which translation you used. Decision record (templating / connections): `preserved` or `not preserved: <why>` (for example `the interval is the run time`).
Limits: partitioned assets change how the job is launched (one partition per run); a Jinja expression the table does not cover is `not preserved`, not guessed.

```python  # templating_config.py (tested)
"""Pattern: Jinja templates, params, Variables, connections. Dagster has no Jinja: translate each template field into code (the converter's set: ds, ds_nodash, ts,
logical_date, data_interval_start/end, ds_add, ds_format; use the same translations, not new ones).
  {{ ds }}, {{ data_interval_start }}/_end  -> `context.partition_key` and `context.partition_time_window` (verified) of a time-partitioned asset: ADD a `DailyPartitionsDefinition` when the DAG depends on the interval;
                                              unpartitioned assets only have the run time (`datetime.now(utc)` or the schedule tick tag): say that the interval is not the same thing.
  {{ params.x }}                            -> a `dg.Config` subclass argument (verified), with the DAG's param default as the field default.
  {{ var.value.x }}, Variable.get("x")      -> an environment variable read by name (`dg.EnvVar("X")` in a resource or config), never a value in code.
  connections                               -> a `ConfigurableResource` whose fields are `dg.EnvVar`s, injected as an asset argument and replaceable by a mock in tests (verified). Names only; no secret value.
"""
import datetime as dt
import os

import dagster as dg

daily = dg.DailyPartitionsDefinition(start_date="2024-01-01")


@dg.asset(partitions_def=daily)
def interval_asset(context: dg.AssetExecutionContext) -> dict:
    tw = context.partition_time_window
    return {"ds": context.partition_key, "ds_add_7": (tw.start + dt.timedelta(days=7)).strftime("%Y-%m-%d"), "data_interval_start": tw.start.isoformat(), "data_interval_end": tw.end.isoformat()}


class RunParams(dg.Config):
    include_roster: bool = True                                         # params.include_roster, default from the DAG


@dg.asset
def parametrised(config: RunParams) -> bool:
    return config.include_roster


class PartnerApi(dg.ConfigurableResource):
    base_url: str                                                       # the Airflow connection's host
    token: str                                                          # its password: always from the environment

    def describe(self) -> str:
        return self.base_url


@dg.asset
def uses_connection(partner_api: PartnerApi) -> str:
    return partner_api.describe()


resources = {"partner_api": PartnerApi(base_url=dg.EnvVar("PARTNER_API_BASE_URL"), token=dg.EnvVar("PARTNER_API_PASSWORD"))}
defs = dg.Definitions(assets=[interval_asset, parametrised, uses_connection], resources=resources)
```

## 4. Where to write it

- The module `rw_pkg.defs.partner_ingest` (`src/rw_pkg/defs/partner_ingest.py`), plus any component folders under `src/rw_pkg/defs/`. Define a job named `partner_ingest` over the DAG's assets and the schedule from the spec.
- The first lines of the module are a docstring that **accounts for every Airflow task**: one line per task id in the form `<task_id>: replaced by asset `<asset key>`` / `folded into `<asset key>`` /
  `dropped: <reason>` / `merged into ...`, followed by the registry components and tiers you used and any behaviour you could not preserve (stated, not hidden).
- Under the accounting, a **decision record**: a paragraph that starts with the line `Decision record:` and then one line per non-obvious behaviour in the form `- <behaviour>: preserved` or
  `- <behaviour>: not preserved: <why>`. Behaviours: `skip/branch`, `trigger rules`, `retries`, `pools`, `sensor modes`, `depends_on_past`, `failure semantics`, `mapped tasks`. Write a line for every behaviour the spec
  shows (the gate lists which it expects); `not preserved` needs a reason, and where folding loses per-step retries or partial restart, name the alternative (`RetryPolicy`, a graph asset). Declaring a loss is accepted;
  hiding it is not. In a many-tasks-into-one-asset rewrite the accounting is still one line per task: `<task_id>: folded into `<asset>``.
- Secrets: environment variables and resources only. No password, token, key or URL with credentials in the code or YAML.

## 5. The acceptance gates

Your rewrite is accepted only when `python3 scripts/accept_rewrite.py --spec rewrite/partner_ingest.spec.json --module rw_pkg.defs.partner_ingest --project <project root> [--defs-dir <component folder>] [--parity <Airflow results>] --manifest manifest.json`
passes every gate (nine):

1. `dg check defs` passes (or, where `dg` is not installed, the definitions load; the gate says which);
2. the generated pytest passes: the module loads, a job named `partner_ingest` exists, the schedule's cron and timezone equal Airflow's, and for a DAG without external systems the DAG materializes and every asset check passes;
3. asset checks are present where the source supports them (at least 2 here);
4. parity: where an Airflow baseline exists, the same overall outcome and the same value as Airflow for every task the header says is `replaced by` an asset (a task `folded into` another has no value of its own to compare);
5. no secret values;
6. no invented APIs: every import and every component `type:` resolves;
7. every Airflow task is accounted for in the header docstring;

8. **task states**: where an Airflow baseline is available (`tools/baseline/`, see `TASK.md`), run the project and compare each task's final state (success, failed, skipped, upstream_failed) and the observable effects
   with Airflow's; the gate names the task that differs. A task folded into another asset with Airflow state `skipped` (a branch not taken) is an accepted representation difference ONLY when the decision record declares it;
9. **decision record**: the header has the `Decision record:` section and it covers every behaviour the spec flags.

A rewrite that fails a gate stays deferred, with the failure recorded; fix it and run the gates again. Never mark a unit complete yourself.

## 6. The converter's output (reference and fallback)

The converter's output for this DAG is at `converter_output/partner_ingest.py` (status `converted`; stubbed tasks: fetch_export, wait_for_export). Read it as a draft: it shows the source's behaviour translated one task at a time. Do not copy its structure; use it to check that you have not lost behaviour.

If the rewrite cannot pass the gates, the converter's output is what ships (or the DAG stays deferred where the converter deferred it). Say plainly in your final message what you could not preserve or verify.

## 7. Cost

This route spends model effort on every DAG (reading the spec, searching the registry, writing and iterating until the gates pass). Use it for the DAGs where idiomatic output or wider operator coverage matters; the
converter stays the default for mechanical DAGs.
