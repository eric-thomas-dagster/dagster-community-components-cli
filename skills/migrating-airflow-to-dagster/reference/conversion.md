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

## Translated, not stubbed

- **`PythonVirtualenvOperator`, `ExternalPythonOperator`, their decorators and the branch variants:** the callable (and the support code it needs) runs in its own interpreter through `af_isolated`: the given `python`, or a virtualenv built once from `requirements` (`uv` when present, else `venv` + `pip`) and cached; arguments and the result are pickled, as Airflow does. An external `python` path from the Airflow DAG is carried over and noted in the report.
- **`TriggerDagRunOperator` without `wait_for_completion`:** the run is created and executed in the background without waiting (the same split the registry's `trigger_job_asset` `wait: false` uses).
- **Task `sla`:** the step records an `sla_breach` observation on its asset when it finishes later than the SLA after the scheduled tick (`dagster/scheduled_execution_time`; the run start for a manual run). The task is not stopped, as in Airflow.
- **`.map(f)` that raises `AirflowSkipException`:** that item is left out, as Airflow skips that mapped instance.

## Settings that map or degrade

- `retry_exponential_backoff` becomes `Backoff.EXPONENTIAL`; a named XCom of a `multiple_outputs` task reads that key of the returned dict.
- Not enforced here, so a `degraded` caveat (the DAG converts and the report says what is missing): `depends_on_past`, `wait_for_downstream`, `max_active_tis_per_dagrun`, `pool_slots`, `execution_timeout` on a Python task, `fail_stop`, `max_consecutive_failed_dag_runs`, `render_template_as_native_obj`, task callbacks that cannot be attached, and `reset_dag_run` / `logical_date` on a trigger.
- Two DAGs that produce the same asset: the second DAG's output keeps its own asset under `<key>/by_<dag>`, with a caveat.
- Asset schedules with nested AND/OR become the same combination of automation conditions. A graph-form DAG (mapping, branching, setup/teardown) triggered by assets runs on an automation condition on those assets; a graph asset cannot declare dependencies, so that lineage edge is not drawn.

## Also translated

- **Mapped provider operators** (S3 copy, delete, list, upload; Snowflake COPY INTO and SQL) in graph DAGs: the step merges the operator's fixed arguments with each expanded item at run time.
- **Async callables** run with `asyncio.run`; `templates_dict` is rendered and passed when the callable takes it; `task_instance_key_str` and `literal()` strings work.
- **Odd schedules**: `ContinuousTimetable` becomes a schedule every minute and `AfterWorkdayTimetable` a weekday cron, each with a degraded caveat. `catchup=True` converts to a schedule from now on, with a caveat, unless `--caveats off`.
- **Unresolved asset aliases** convert without a trigger, with a caveat; unordered writers of one asset each keep their own asset.

## Task-group loops (Airflow 3.4 `group.loop(max_iterations=, until=)`)

A loop group (its Python tasks plus the gate) becomes one task. Its callable runs the group's tasks in order once per iteration, handing each task the `loop` object (`loop.index` from 1, `loop.previous` the preceding iteration's result, `loop.result` for the `until` callable), and consults `until` after each iteration: a `LoopDecision`, a bool or a `(stop, reason)` pair. With no `until` the body runs exactly `max_iterations` times; with one, running out of iterations before it says stop fails the step, as in Airflow. Tasks after the loop depend on the one asset. A loop whose body has a mapped task, a non-Python task, or a task fed from outside the group stays a stub. Checked on Airflow's own examples: Newton's square root converges in the same iterations, a fixed 3-iteration loop returns 3, and Airflow and Dagster both succeed.

## Runtime-partitioned assets (Airflow 3.3 `PartitionedAtRuntime`)

A producer that records its partition keys while it runs converts to its own asset (`<dag>/<task>`) plus a dynamically partitioned catalog asset at the outlet's key: `outlet_events[asset].add_partitions(key or keys)` registers each key as a dynamic partition and reports a materialization for it (duplicates collapse), and a run launched with the tag `airflow/partition_key` records that key when the step succeeds. A consumer with the identity mapping gets the same dynamic partition set and an eager automation condition. A reference to an asset by name (`Asset.ref(name=...)`) resolves to the producer's key.

## Still deferred (8)

Consumers of runtime-partitioned assets with a rollup, fan-out, product or allowed-key mapping (5 DAGs, Airflow 3.3 only), a timetable with a mapper per asset (1), a mapped task that combines several argument shapes (1), and a mapped task whose fixed arguments are not plain values (1).

## `--output components`: registry components instead of generated Python

`convert_ir.py --output components` writes `defs/<dag>/defs.yaml` for each DAG whose every task has a component form, and keeps generated Python for the rest (the report's `output` field says which). The YAML holds one component per task (documents separated by `---`) and a `CronScheduleComponent` for the schedule:

| Airflow | Component |
|---|---|
| `BashOperator` | `ShellCommandAssetComponent` (retries, timeout, env, cwd, `skip_on_exit_code: 99`) |
| `PythonOperator`, `@task` | `PythonCallableAssetComponent` (the callable is copied to `callables/<dag>.py`) |
| S3 copy, delete (key list), list, upload, create object | the matching `S3...AssetComponent` with `resource_key` set to the connection id |
| `EmptyOperator` | none: dependencies are rewired through it |

A DAG stays Python when any task is templated (the component YAML has its own Jinja), consumes another task's value, is mapped, gated, or a branch, has outlets or inlets, a pool, an SLA, callbacks or a caveat, or when its schedule is anything but a cron or none. Asset keys in this form are single-part, `<dag>__<task>`, because the components name their asset with one string. A DAG without a schedule gets no job. On the example corpus 18 DAGs convert this way and load and run in a Dagster project; the rest stay Python. Snowflake, triggers, and anything that needs `post_processing` are the next candidates.

## Caveats: convert the shape, mark what is not translated

By default (`--caveats raise`) a DAG with one untranslatable task keeps its shape and the task is written as a stub: an asset in its place whose body calls `af_caveat(reason)`, with an `# AIRFLOW-CAVEAT:` comment. It applies to a callable that uses the Airflow API, an unresolved import or an async function, an operator with no conversion yet (provider operators, `PythonVirtualenvOperator`, human-in-the-loop operators), `templates_dict`, and templates that use names the runtime does not rebuild. The report lists each one under the DAG's `caveats`, and `apply_conversion.py` leaves that task's unit `deferred` with the reason.

Two kinds of caveat appear in the report: `stub` (the task body is not translated) and `degraded` (translated, with a known difference: for example the settings listed in the table below, a dropped `ExternalTaskSensor` wait that the converter cannot express, or DAG-level callbacks that were not attached). Each unconverted operator names the registry component that covers the same work (`OPERATOR_COMPONENTS` in `convert_ir.py`).

- `--caveats raise` (default): the stub fails when it runs, with the reason in the message, so nothing succeeds by accident.
- `--caveats skip`: the stub returns nothing and the rest of the DAG runs. Use it to see the shape run end to end; read the caveat list before trusting any output.
- `--caveats off`: no stubs and no degraded caveats; the whole DAG is deferred with the reason instead.

Whole-DAG problems still defer the DAG: producer conflicts, schedule shapes, a SubDAG, a partitioned DAG with steps that are not converted, a custom operator class that cannot be copied.

## What it defers, always with the reason

A whole DAG is deferred only when part of it cannot be expressed at all (a partial conversion would silently change the graph): an asset produced by unordered tasks or by two DAGs in a way it cannot split, a SubDAG, a schedule shape with no mapping (a timetable with a mapper per asset, custom timetables), `catchup=True` (unless `--catchup ignore`), a custom operator class that cannot be copied, an operator argument that is not a plain value, the mapped or partitioned shapes under "Still deferred", and any callable whose arguments cannot be wired (with `--caveats off`). Jinja templates, task context, dynamic mapping, trigger rules, setup/teardown and callbacks are translated (see above and the settings table below), not deferred. Everything else that cannot be translated is a loud stub or a `degraded` caveat (next section) unless `--caveats off`, which turns each of them into a deferral of the whole DAG. A converter bug shows up as a deferral named `CONVERTER ERROR`, never a crash or a silent skip.

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

## Task and DAG settings: translated, degraded or deferred, never ignored

Loading a converted project proves nothing about behavior. Running both sides (below) found settings the converter had silently dropped, so every setting that changes what happens is now handled explicitly. "Degraded" is a caveat in the report (the DAG converts and the report says what is not enforced); with `--caveats off` each degraded setting defers the DAG.

| Setting | Handling |
|---|---|
| `retries`, `retry_delay`, `retry_exponential_backoff`, `pool`, task groups | translated (`RetryPolicy` with `Backoff.EXPONENTIAL`, `pool`, key prefix) |
| `execution_timeout` on a shell task | translated (`subprocess` timeout; the task fails, as in Airflow) |
| `execution_timeout` on a Python task | degraded (a callable cannot be interrupted safely) |
| `trigger_rule` other than `all_success` | translated in the graph form (Airflow's own rule table); a rule the table does not have defers the DAG |
| `sla` on a task | translated (an `sla_breach` observation; the task is not stopped, as in Airflow) |
| `depends_on_past`, `wait_for_downstream` | degraded (not enforced) |
| `max_active_tis_per_dagrun`, `pool_slots != 1` | degraded (not enforced; one slot is used) |
| `max_active_tis_per_dag` | translated to a Dagster pool of that size (the limit goes in `concurrency_limits.yaml`); defers with a named pool or a value that is not a positive number |
| task callbacks (`on_failure_callback`, `on_success_callback`, `on_retry_callback`) | translated with `@on_hooks` in the asset form; degraded in the graph form |
| other hooks (`pre_execute=`, `post_execute=`, `@task.run_if`, `skip_if`, `on_skipped_callback`, `on_execute_callback`) | degraded (Airflow keeps the hook in `_pre_execute_hook`; the default no-op methods are not hooks) |
| DAG `fail_stop=True` | degraded (Airflow cancels running tasks on the first failure; Dagster lets independent steps finish) |
| DAG `max_active_runs` / `max_active_tasks` other than 16 | translated to a run-queue tag limit and a per-run executor limit written to `concurrency_limits.yaml` for the deployment to apply; a value that is not a positive number defers |
| DAG `dagrun_timeout` | translated to a `dagster/max_runtime` tag (enforced only with run monitoring); defers when not a number of seconds |
| DAG `max_consecutive_failed_dag_runs`, `render_template_as_native_obj` | degraded |

Detection traps this exposed: Airflow 2's decorated mapped tasks are `DecoratedMappedOperator`, a subclass of `MappedOperator`, so test the class hierarchy, not the class name; and `pre_execute` / `post_execute` exist on every operator as no-op methods, so only an override or a stored hook counts.

## Execution parity

`examples/runtime_conversion/parity/` runs each eligible converted DAG twice: with Airflow's own `dag.test()` in a sandbox, and by materializing the converted assets in Dagster. It compares every task's outcome and, where Dagster stores a value, the value passed between tasks. Eligible means no external resources and no network or long sleeps in the commands. Pitfalls in the harness itself:

- Resolve every output path before changing into the temp working directory the DAGs run in.
- A per-DAG time limit that is shorter than the DAG's total sleeps interrupts both sides at different points and looks like a behavior difference; set the limit above the DAG's real duration.
- Airflow 3's `dag.test()` needs the DAG serialized into the database first (`sync_bag_to_db`) and a migrated database; Airflow 2 needs `initdb()`.

## Measured results

Corpus: 292 DAGs from 14 public repos (heavy on teaching repos for exactly the constructs that need a design decision, so do not read the conversion rate as typical).

- **Conversion:** 284 of 292 DAGs convert (59 of them with at least one stubbed task) (`--caveats off` defers every DAG that would need a stub or a degraded setting; the report counts each kind) (partitioned DAGs are not in the parity comparison below: they need a partition to run). The rest are deferred, each with its reasons in `report.json`. Every converted repo loads in a real Dagster project (`load_check2.py`). Concurrency is mapped to Dagster concepts and written to a generated `concurrency_limits.yaml` that the deployment must apply: `max_active_runs` becomes a run-queue tag limit, a named pool or `max_active_tis_per_dag` becomes a Dagster pool (limit set with `dagster instance concurrency set`), and `max_active_tasks` becomes a per-run executor limit (exact only when runs are limited to one at a time). `pool_slots` above 1 and `max_active_tis_per_dagrun` are degraded caveats (not enforced). These mappings are checked by loading and by unit tests; the pool and queue limits were not exercised under a running daemon.
- **Execution parity** (Airflow's own `dag.test()` against Dagster materialization, on converted DAGs with no external resources and no network or long sleeps): 117 DAGs, 115 compared and 2 not comparable (Airflow itself failed in the harness: a deadline-alert serialization error, and a param with no default). Of the 115, 106 had identical outcomes. The other 9 differ only in values that cannot match: random picks (five, one of them a random dog picture from a web API), timestamps (two), file listing times (one) and a tuple that Airflow's XCom stores as a tagged dict (one). Covered: branches and short-circuits, trigger rules, setup/teardown, mapped tasks, polling sensors, clock branches and triggered DAGs, in Airflow 2.10 and 3.4.
- **Every converted DAG run in Dagster** (all 284, in-process with an ephemeral instance; partitioned DAGs given a partition key): 194 ran clean; 48 stopped at a marked caveat stub (the loud `AIRFLOW-CAVEAT` failure, as designed); 26 failed for reasons outside the converter (DAGs that fail on purpose, a network call, a data file or a package the example assumes, a failed upstream in a failure-path demo); 16 were only loaded (they need AWS or Snowflake credentials). This pass found and fixed: `templates_dict` read through `**kwargs`, `self` for `@asset` functions, `outlet_events`/`inlet_events` keyed by a URI string and `inlet_events.for_asset(...)`, the background `TriggerDagRunOperator` launch, a trigger whose target DAG is not in the converted set (now a stub), and loop-body callables that are left unexpanded (now stubs, as are callables that take `asset_state_store`).
- **Passed values:** 435 task outcomes and 165 returned values were compared; 155 of the values were identical, and the 10 that differ are the cases above. For a mapped task Airflow records one instance and Dagster the collected list, so that check passes if Airflow's value appears in the list (a loose rule). Skipped, failed and branch markers are not compared as values. Nothing here ran against real S3 or Snowflake. Schedules were checked structurally (cron, timezone), not by waiting for ticks. The failure paths (a task that fails, a failed setup or teardown) are covered by unit tests, not by comparing against Airflow running a failing DAG.
- **Bugs the comparison found that loading did not** (all fixed and covered by tests): decorated mapped tasks misdetected, `execution_timeout` ignored, `fail_stop` ignored, `@task.run_if` / `skip_if` hooks ignored, a harness timeout that looked like a behavior difference, and (found in the second pass) unsupported `macros.*` calls, `dag_run` attributes the runtime does not provide, and `templates_dict`, all of which were accepted at convert time and failed at run time; and, in the third pass, a shell task's output (the last stdout line is its XCom, parsed as JSON on Airflow 3), a pull of a task id that does not exist (Airflow returns None), and `do_xcom_push=False`; and, in the fourth pass, weekday branches given as numbers, and short-circuits that keep downstream trigger rules (`ignore_downstream_trigger_rules=False`). The incremental runner hashes a DAG's generated module, so a change to the shared runtime needs `--all` or the affected DAGs forced. Expect the first run on your own repo to find more; run the comparison.
