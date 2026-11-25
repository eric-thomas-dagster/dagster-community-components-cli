# Rewrite Airflow DAG `toy_taskflow_bash` as idiomatic Dagster

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

- The module `rs_pkg.defs.toy_taskflow_bash` (`src/rs_pkg/defs/toy_taskflow_bash.py`), plus any component folders under `src/rs_pkg/defs/`. Define a job named `toy_taskflow_bash` over the DAG's assets and the schedule from the spec.
- The first lines of the module are a docstring that **accounts for every Airflow task**: one line per task id in the form `<task_id>: replaced by asset `<asset key>`` / `folded into `<asset key>`` /
  `dropped: <reason>` / `merged into ...`, followed by the registry components and tiers you used and any behaviour you could not preserve (stated, not hidden).
- Secrets: environment variables and resources only. No password, token, key or URL with credentials in the code or YAML.

## 5. The acceptance gates

Your rewrite is accepted only when `python3 scripts/accept_rewrite.py --spec rewrite/toy_taskflow_bash.spec.json --module rs_pkg.defs.toy_taskflow_bash --project <project root> [--defs-dir <component folder>] [--parity <Airflow results>] --manifest manifest.json`
passes every gate:

1. `dg check defs` passes (or, where `dg` is not installed, the definitions load; the gate says which);
2. the generated pytest passes: the module loads, a job named `toy_taskflow_bash` exists, the schedule's cron and timezone equal Airflow's, and for a DAG without external systems the DAG materializes and every asset check passes;
3. asset checks are present where the source supports them (at least 1 here);
4. parity: where an Airflow baseline exists, the same overall outcome and the same value as Airflow for every task the header says is `replaced by` an asset (a task `folded into` another has no value of its own to compare);
5. no secret values;
6. no invented APIs: every import and every component `type:` resolves;
7. every Airflow task is accounted for in the header docstring.

A rewrite that fails a gate stays deferred, with the failure recorded; fix it and run the gates again. Never mark a unit complete yourself.

## 6. The converter's output (reference and fallback)

The converter's output for this DAG is at `sample/converter_output/toy_taskflow_bash.py` (status `converted`; stubbed tasks: none). Read it as a draft: it shows the source's behaviour translated one task at a time. Do not copy its structure; use it to check that you have not lost behaviour.

If the rewrite cannot pass the gates, the converter's output is what ships (or the DAG stays deferred where the converter deferred it). Say plainly in your final message what you could not preserve or verify.

## 7. Cost

This route spends model effort on every DAG (reading the spec, searching the registry, writing and iterating until the gates pass). Use it for the DAGs where idiomatic output or wider operator coverage matters; the
converter stays the default for mechanical DAGs.
