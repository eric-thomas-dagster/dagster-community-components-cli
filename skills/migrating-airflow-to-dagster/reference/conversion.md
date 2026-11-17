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
