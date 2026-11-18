# Converting DAGs mechanically: runtime IR, then code generation

For the constructs that have a mechanical mapping, do not hand-convert DAG by DAG. Extract what Airflow actually built, then generate Dagster code and let the gates check it.

```
airflow_ir.py   (sandbox: runs the DAG files like Airflow's parser)  ->  ir.json
convert_ir.py   (deterministic)                                       ->  defs/<dag>.py, runtime.py, resources.py, sources.py, report.json
apply_conversion.py                                                    ->  manifest targets (converted) or deferrals (with the reason)
validate_defs.py / status.py                                           ->  the existing gates and completeness check
```

## Why start from the runtime view

Loops, factories, dag-factory YAML, Blueprints and dynamic DAG generation only exist once the code runs. Airflow's DagBag has already expanded them, so the IR holds every task with resolved arguments, dependencies, schedule, retries, pool, task groups and the callable's source. A loop-generated DAG is then as easy to convert as a plain one. The cost: `airflow_ir.py` executes the DAG files. Run it only on code you trust, in a sandbox, with the same Airflow version and providers as the project.

## What `convert_ir.py` converts

| Airflow | Dagster |
|---|---|
| `PythonOperator`, `@task` | asset running the same function; the function's source is copied together with exactly the imports, constants and helper functions it uses (nothing from Airflow) |
| `BashOperator`, `@task.bash` | asset running the command with `bash -c` |
| `EmptyOperator` | elided; its edges are rewired |
| values returned from one task and passed to another | asset parameters (the IO manager carries them) |
| `S3ListOperator`, `S3CopyObjectOperator`, `S3DeleteObjectsOperator` (explicit keys), `LocalFilesystemToS3Operator` | assets calling boto through the `aws_*` resource, mirroring what the hook calls |
| `CopyFromExternalStageToSnowflakeOperator`, `SnowflakeOperator`, `SQLExecuteQueryOperator` on a Snowflake connection | assets running the same SQL through the Snowflake resource; operator-level warehouse/database/role/schema become `USE` statements |
| one task with one outlet | the asset with that key (`s3://b/x` becomes `s3/b/x`); inlets are dependencies |
| tasks in a chain that name the same outlet | the last one produces the asset; the earlier ones are plain assets (note recorded) |
| schedule: none, `@once`, cron (trigger or data-interval timetable) | no schedule or `ScheduleDefinition` with the timezone |
| schedule: one asset, or any of several | `AutomationCondition.eager()` |
| schedule: all of several assets | per-asset `asset_matches(...newly_updated()).since_last_handled()` on the DAG's root tasks (verified: fires only after every upstream updated, not on a repeat update of one) |
| task groups, retries, retry delay, pool | key prefix, `RetryPolicy`, `pool` |

Connections become resources under the same key (`resources.py`); third-party imports the copied code needs are listed in `converted_requirements.txt`; assets a converted DAG reads but no converted DAG produces become `sources.py`.

## What it defers, always with the reason

Jinja templates, task context (`ds`, `ti`, `**context`, `dag_run`), other operators, dynamic task mapping, trigger rules other than `all_success`, setup/teardown, callbacks, `catchup=True`, other timetables (delta, partitioned assets), assets written by unordered tasks, an asset already produced by another DAG, and any callable that cannot be copied faithfully (lambdas, Airflow APIs, unresolved names). A DAG is converted whole or not at all: a partial conversion would silently change the graph. A converter bug shows up as a deferral named `CONVERTER ERROR`, never a crash or a silent skip.

## Rules this encodes (see also factories.md and dynamic-mapping.md)

1. One producer per asset. Chained writers of one outlet collapse to the last; unordered writers need an owner decision.
2. A task that reads and writes the same asset cannot depend on itself; the self-dependency is dropped and noted.
3. Multi-asset AND triggers need a per-asset condition, not `eager()` (which fires on any update).
4. Data passing is the IO manager's job. Check what your deployment's default IO manager does when steps run in different processes.

## Existing integrations and community components

Prefer an official `dagster-*` integration or a registry component over hand-written helpers (the dagster-expert skill's rule). `convert_ir.py` writes an `operators` section into `report.json`: every operator type in the IR with its task count, whether the converter maps it, and the integration or component that fits. Component ids below were found with `dagster-component search "<term>" --json`; confirm with `dagster-component info <id>` before relying on them.

| Airflow | Official integration | Registry component | What is missing |
|---|---|---|---|
| `BashOperator`, `@task.bash` | none usable: `dagster-shell` on PyPI is stale (0.25.13, pins `dagster==1.9.13`) | `shell_command_asset` (works: it calls `subprocess` itself) | retries, pool, multi-part asset key, automation condition, metadata attributes |
| S3 operators | `dagster-aws` `S3Resource` (attaches the boto client) | `aws_s3_resource`, `s3_cleanup_job`, `external_s3_asset` | list-objects, copy-object, upload-file and delete-by-key assets |
| Snowflake operators | `dagster-snowflake` `SnowflakeResource` (attaches a `SnowflakeConnection`) | `snowflake_resource`, `snowflake_stored_procedure_call_asset` | a run-SQL-statements asset (multi-statement split, `USE` overrides) and a COPY INTO asset |
| `KubernetesPodOperator` | `dagster-k8s` `PipesK8sClient` | `k8s_job_asset` | not mapped by the converter yet |
| HTTP operators | none | `http_resource`, `http_poll_sensor` | a request-as-an-asset component |
| `TriggerDagRunOperator` | none | `asset_to_job_trigger_sensor` | usually an asset dependency or automation condition instead |
| `PythonVirtualenvOperator`, `ExternalPythonOperator` | Dagster Pipes (`PipesSubprocessClient`) | none | run-a-callable-in-a-virtualenv |
| time and task sensors | none | none | time-of-day wait; a sensor on a task state in another job |

The converter currently emits plain Python assets so converted code has no dependency on the components repo. Switching a handler to emit a component's YAML is the natural next step once the component carries the attributes the conversion needs (see "what is missing"). The generated code uses `dagster-aws` and `dagster-snowflake` resources directly.

## Compatibility shim: S3Hook

Callables that construct `S3Hook` themselves cannot reach a Dagster resource. The generated `runtime.py` includes an `S3Hook` shim for the commonly used methods (`load_file`, `load_string`, `load_bytes`, `check_for_key`, `list_keys`, `delete_objects`, `copy_object`). It was checked against Airflow's real hook with a recording fake boto client: 11 of 11 scenarios identical (calls, return values and errors). It uses the default AWS credential chain, and `read_key` (boto's resource API), compression, encryption and ACL options raise `NotImplementedError` instead of behaving differently. A converted DAG that uses it carries a `NOTE:` in its module.

## Task and DAG settings: mapped or deferred, never ignored

Loading a converted project proves nothing about behavior. Running both sides (below) found settings the converter had silently dropped, so every setting that changes what happens is now handled explicitly:

| Setting | Handling |
|---|---|
| `retries`, `retry_delay`, `pool`, task groups | mapped (`RetryPolicy`, `pool`, key prefix) |
| `execution_timeout` on a shell task | mapped (`subprocess` timeout; the task fails, as in Airflow) |
| `execution_timeout` on a Python task | deferred (a callable cannot be interrupted safely) |
| `trigger_rule` other than `all_success` | deferred (see the component wishlist) |
| `depends_on_past`, `wait_for_downstream`, `retry_exponential_backoff`, SLAs | deferred |
| `max_active_tis_per_dag`, `max_active_tis_per_dagrun`, `pool_slots != 1` | deferred |
| task callbacks (`on_*_callback`) and hooks (`pre_execute=`, `post_execute=`, `@task.run_if`, `skip_if`) | deferred (Airflow keeps the hook in `_pre_execute_hook`; the default no-op methods are not hooks) |
| DAG `fail_stop=True` | deferred (Airflow cancels running tasks on the first failure; Dagster lets independent steps finish) |
| DAG `max_active_runs` / `max_active_tasks` / `concurrency` other than 16 | deferred (a run-queue limit is deployment configuration, not code; a converter could tag the job and emit the limit for the deployment) |
| DAG `dagrun_timeout`, `max_consecutive_failed_dag_runs`, `render_template_as_native_obj` | deferred |

Detection traps this exposed: Airflow 2's decorated mapped tasks are `DecoratedMappedOperator`, a subclass of `MappedOperator`, so test the class hierarchy, not the class name; and `pre_execute` / `post_execute` exist on every operator as no-op methods, so only an override or a stored hook counts.

## Execution parity

`examples/runtime_conversion/parity/` runs each eligible converted DAG twice: with Airflow's own `dag.test()` in a sandbox, and by materializing the converted assets in Dagster. It compares every task's outcome and, where Dagster stores a value, the value passed between tasks. Eligible means no external resources and no network or long sleeps in the commands. Pitfalls in the harness itself:

- Resolve every output path before changing into the temp working directory the DAGs run in.
- A per-DAG time limit that is shorter than the DAG's total sleeps interrupts both sides at different points and looks like a behavior difference; set the limit above the DAG's real duration.
- Airflow 3's `dag.test()` needs the DAG serialized into the database first (`sync_bag_to_db`) and a migrated database; Airflow 2 needs `initdb()`.

## Measured results

Corpus: 292 DAGs from 14 public repos (heavy on teaching repos for exactly the constructs that need a design decision, so do not read the conversion rate as typical).

- **Conversion:** 137 of 292 DAGs convert mechanically (partitioned DAGs are not in the parity comparison below: they need a partition to run). The rest are deferred, each with its reasons in `report.json`. Every converted repo loads in a real Dagster project (`load_check2.py`). Concurrency is mapped to Dagster concepts and written to a generated `concurrency_limits.yaml` that the deployment must apply: `max_active_runs` becomes a run-queue tag limit, a named pool or `max_active_tis_per_dag` becomes a Dagster pool (limit set with `dagster instance concurrency set`), and `max_active_tasks` becomes a per-run executor limit (exact only when runs are limited to one at a time). `pool_slots` above 1 and `max_active_tis_per_dagrun` are still deferred. These mappings are checked by loading and by unit tests; the pool and queue limits were not exercised under a running daemon.
- **Execution parity** (Airflow's own `dag.test()` against Dagster materialization, on converted DAGs with no external resources and no network or long sleeps): 117 DAGs, 115 compared and 2 not comparable (Airflow itself failed in the harness: a deadline-alert serialization error, and a param with no default). Of the 115, 106 had identical outcomes. The other 9 differ only in values that cannot match: random picks (five, one of them a random dog picture from a web API), timestamps (two), file listing times (one) and a tuple that Airflow's XCom stores as a tagged dict (one). Covered: branches and short-circuits, trigger rules, setup/teardown, mapped tasks, polling sensors, clock branches and triggered DAGs, in Airflow 2.10 and 3.4.
- **Passed values:** 435 task outcomes and 165 returned values were compared; 155 of the values were identical, and the 10 that differ are the cases above. For a mapped task Airflow records one instance and Dagster the collected list, so that check passes if Airflow's value appears in the list (a loose rule). Skipped, failed and branch markers are not compared as values. Nothing here ran against real S3 or Snowflake. Schedules were checked structurally (cron, timezone), not by waiting for ticks. The failure paths (a task that fails, a failed setup or teardown) are covered by unit tests, not by comparing against Airflow running a failing DAG.
- **Bugs the comparison found that loading did not** (all fixed and covered by tests): decorated mapped tasks misdetected, `execution_timeout` ignored, `fail_stop` ignored, `@task.run_if` / `skip_if` hooks ignored, a harness timeout that looked like a behavior difference, and (found in the second pass) unsupported `macros.*` calls, `dag_run` attributes the runtime does not provide, and `templates_dict`, all of which were accepted at convert time and failed at run time; and, in the third pass, a shell task's output (the last stdout line is its XCom, parsed as JSON on Airflow 3), a pull of a task id that does not exist (Airflow returns None), and `do_xcom_push=False`; and, in the fourth pass, weekday branches given as numbers, and short-circuits that keep downstream trigger rules (`ignore_downstream_trigger_rules=False`). The incremental runner hashes a DAG's generated module, so a change to the shared runtime needs `--all` or the affected DAGs forced. Expect the first run on your own repo to find more; run the comparison.
