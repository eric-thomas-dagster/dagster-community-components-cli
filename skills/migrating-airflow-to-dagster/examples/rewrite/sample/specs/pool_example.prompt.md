# Rewrite Airflow DAG `pool_example` as idiomatic Dagster

You are writing the Dagster code for one Airflow DAG from scratch. Do not transform the Airflow source line by line: understand what the DAG does from the specification below, then build what a Dagster
engineer would build for that behaviour. The converter's output is a draft and the fallback if your rewrite fails the gates; it is not the model to imitate.

## 1. Load the skills and follow them

1. Load the **`dagster-expert`** skill (by name) and follow its component hierarchy: components and registry/official integrations before hand-written code; create the project with
   `uvx create-dagster project rs_project --uv-sync` if it does not exist, never by hand; scaffold with `dg scaffold defs`; use its references for assets, schedules, sensors, automation and resources. Check every Dagster
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

- The module `rs_pkg.defs.pool_example` (`src/rs_pkg/defs/pool_example.py`), plus any component folders under `src/rs_pkg/defs/`. Define a job named `pool_example` over the DAG's assets and the schedule from the spec.
- The first lines of the module are a docstring that **accounts for every Airflow task**: one line per task id in the form `<task_id>: replaced by asset `<asset key>`` / `folded into `<asset key>`` /
  `dropped: <reason>` / `merged into ...`, followed by the registry components and tiers you used and any behaviour you could not preserve (stated, not hidden).
- Secrets: environment variables and resources only. No password, token, key or URL with credentials in the code or YAML.

## 5. The acceptance gates

Your rewrite is accepted only when `python3 scripts/accept_rewrite.py --spec rewrite/pool_example.spec.json --module rs_pkg.defs.pool_example --project <project root> [--defs-dir <component folder>] [--parity <Airflow results>] --manifest manifest.json`
passes every gate:

1. `dg check defs` passes (or, where `dg` is not installed, the definitions load; the gate says which);
2. the generated pytest passes: the module loads, a job named `pool_example` exists, the schedule's cron and timezone equal Airflow's, and for a DAG without external systems the DAG materializes and every asset check passes;
3. asset checks are present where the source supports them (at least 0 here);
4. parity: where an Airflow baseline exists, the same overall outcome and the same value as Airflow for every task the header says is `replaced by` an asset (a task `folded into` another has no value of its own to compare);
5. no secret values;
6. no invented APIs: every import and every component `type:` resolves;
7. every Airflow task is accounted for in the header docstring.

A rewrite that fails a gate stays deferred, with the failure recorded; fix it and run the gates again. Never mark a unit complete yourself.

## 6. The converter's output (reference and fallback)

The converter's output for this DAG is at `sample/converter_output/pool_example.py` (status `converted`; stubbed tasks: none). Read it as a draft: it shows the source's behaviour translated one task at a time. Do not copy its structure; use it to check that you have not lost behaviour.

If the rewrite cannot pass the gates, the converter's output is what ships (or the DAG stays deferred where the converter deferred it). Say plainly in your final message what you could not preserve or verify.

## 7. Cost

This route spends model effort on every DAG (reading the spec, searching the registry, writing and iterating until the gates pass). Use it for the DAGs where idiomatic output or wider operator coverage matters; the
converter stays the default for mechanical DAGs.
