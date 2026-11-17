# Component and integration enhancements that would convert more Airflow DAGs

Measured on 265 DAGs from 13 public Airflow example repos (the corpus is heavy on teaching repos, so absolute counts overstate how rare these constructs are in production; use the ranking, not the percentages). The converter maps 55 DAGs mechanically. For each unconverted DAG it records every blocker; "alone" below counts DAGs whose only blocker is that category, so it is what one enhancement would unlock by itself.

| # | Blocker | DAGs blocked | alone | What would unlock it |
|---|---|---|---|---|
| 1 | Callables that use the task context (`ds`, `dag_run`, `ti`, `**context`, `params`) | 27 | 27 | an Airflow-context shim for Python components (item A below) |
| 2 | Dynamic task mapping (`.expand` / `.map`) | 30 | 22 | partition-by-items components (item B) |
| 3 | Jinja templates (`{{ ds }}`, `{{ params.x }}`, `{{ ti.xcom_pull(...) }}`) | 24 | 19 | the same context shim plus a template renderer (item A) |
| 4 | Callables that use the Airflow API (`Variable`, `S3Hook` methods not mirrored, `get_current_context`) | 15 | 15 | a variables/connections compatibility layer (item A) |
| 5 | Branching and short-circuit | 26 | 9 | conditional-materialization component (item D) |
| 6 | Trigger rules other than `all_success` | 27 | 0 | an automation-condition library (item C): it removes a blocker everywhere else it appears |
| 7 | Schedules (delta, partitioned assets, `@once` variants) | 20 | 9 | schedule components (item E) |
| 8 | Sensors (time, time-delta, file, bash, python, external task) | 13 | 7 | sensor components (item F) |
| 9 | `TriggerDagRunOperator` | 8 | 6 | job-trigger component (item G) |
| 10 | Virtualenv / external-python callables | 9 | 3 | Pipes-based component (item H) |
| 11 | setup/teardown | 9 | 0 | scoped setup/teardown resource component (item I) |

## A. Python callable component with an Airflow-context shim (largest unlock)

Today the converter copies each callable's source into a generated module. A component would let a project keep the function where it is and still run it from Dagster.

`python_callable_asset`: `callable: "package.module:function"`, `args`, `kwargs`, `inputs` (upstream asset keys passed as arguments, replacing XCom), `context_mode: none | airflow`, plus the common attributes below. With `context_mode: airflow` the component injects only what the callable declares: `ds`, `ds_nodash`, `ts`, `logical_date`, `data_interval_start/end`, `run_id`, `params` (from a `params:` attribute), `dag_run.conf` (from run config), `ti.xcom_pull(task_ids=...)` (resolved to upstream asset values) and `ti.try_number`. Logical date comes from a run tag set by the schedule; the shim's schedule helper must support both Airflow 3 (logical date = tick) and Airflow 2 (logical date = start of the interval that ended). Both rules were verified against Airflow's timetables (see `reference/factories.md`, rules 3 and 4). Jinja rendering: support `ds`, `ds_nodash`, `logical_date | ds`, `params.*` and `var.value.*`; raise at load time for anything else.

A compatibility package (suggested name `airflow_compat`) would also hold what the converter's `runtime.py` generates today: the `S3Hook` shim (checked 11 of 11 scenarios against Airflow's real hook) and a `Variable.get` that reads environment variables, so converted code stops carrying its own copy.

## B. Partition-by-items components (dynamic task mapping)

- `static_partitioned_asset`: one asset partitioned by a list. Replaces N identical DAGs or an `.expand` over a literal list. Verified pattern in `examples/dagfactory_family/`.
- `dynamic_partition_discovery_schedule`: a schedule or sensor that lists items (an S3 prefix, a SQL query, a callable), registers new ones as dynamic partitions and requests one run each, with `run_key` idempotence and a skip when nothing is found. Pattern verified in `examples/s3_to_snowflake_factories/example_04_per_file/`. Include a pruning option (`keep_days`), because dynamic partitions accumulate.

## C. Automation-condition library (trigger rules)

Named, tested conditions that express Airflow trigger rules at the asset level: `all_upstream_done` (run after upstream succeed or fail), `any_upstream_failed`, `none_failed_min_one_success`, `all_of_updated_since_last_run` (the per-asset `asset_matches(...).since_last_handled()` AND used by the converter, verified), and `any_of_updated`. Expose them as strings in component YAML (`automation_condition: all_upstream_done`) and as Python helpers. Each needs a test using `dg.evaluate_automation_conditions` (see `examples/runtime_conversion/parity/and_trigger.py`).

## D. Conditional materialization (branch / short-circuit)

`conditional_asset`: a callable returning a bool (short-circuit) or the name of the branch to run (branch), and a way for downstream assets to be skipped rather than failed. Design needed: asset checks with `blocking=True`, or an automation condition keyed on a metadata flag. Without a decision here these DAGs stay hand-converted.

## E. Schedule components

`airflow_schedule`: cron or preset, timezone, `semantics: trigger | data_interval`, run tags carrying the logical date, optional `end_date`. Delta timetables (`timedelta`, `DeltaTriggerTimetable`) map to cron only when the interval divides the period; otherwise a custom schedule. `@once` as a manual-only job.

## F. Sensor components

`time_of_day_schedule` (replaces `TimeSensor` waits; normally a schedule, not a sensor), `file_sensor` (local and S3 key, with `poke_interval`/`timeout` semantics stated), `python_sensor` (callable returning a bool, then a run request), `external_job_state_sensor` (a sensor on another job's or asset's state, replacing `ExternalTaskSensor`).

## G. Job trigger

`trigger_job_asset`: launch another job with config and optionally wait for it. Verify `asset_to_job_trigger_sensor`: the registry search returns it but it is not in the local `dagster-component-templates` checkout, so the checkout and the registry index may be out of sync.

## H. Virtualenv / external python

`pipes_python_asset`: run a callable in a fresh virtualenv (`requirements`) or a given interpreter through Dagster Pipes (`PipesSubprocessClient`).

## I. Scoped setup and teardown

Airflow runs setup tasks before a scope and teardown tasks after it, even when the scope fails. In Dagster that is a resource lifecycle (a resource that yields). A `scoped_setup_teardown` component would define the resource from two callables or commands and attach it to a list of assets.

## Existing components: attributes to add

`ssh_asset` is the model: it already has retry policy, owners, tags, kinds, partitions and freshness. Bring these to the others through one shared set of common attributes:

| Common attribute | Why the conversion needs it |
|---|---|
| `key` as a multi-part string (`"dag/task"`, `"s3/bucket/path"`) instead of a single `asset_name` | converted assets are keyed by DAG and task, and by asset URI |
| `retry_policy_max_retries`, `retry_policy_delay_seconds`, `retry_policy_backoff` | `retries`, `retry_delay` |
| `pool` | Airflow pools |
| `automation_condition` (named presets, item C) | asset-triggered DAGs and trigger rules |
| `kinds`, `owners`, `tags`, `metadata` | lineage and ownership in the UI |
| `timeout_seconds` | `execution_timeout`; `shell_command_asset` and `k8s_job_asset` have none |

Component-specific:

- **`shell_command_asset`** (the converter's most frequent operator, `BashOperator`/`@task.bash`): add the common attributes; state that commands run with `bash -c` and merge the environment (as Airflow does); add `skip_on_exit_code` (Airflow's default 99); add `timeout_seconds`; support the command arriving from an upstream asset (`@task.bash`). Fix the description and requirements: it says it wraps `dagster-shell`, which is stale (latest 0.25.13, pins `dagster==1.9.13`), while the code uses `subprocess`.
- **`k8s_job_asset`**: common attributes; image pull policy, service account, volumes (what `KubernetesPodOperator` sets); a Pipes variant (`dagster-k8s` `PipesK8sClient`) for logs and metadata streaming.
- **`aws_s3_resource`**: `profile_name`, `role_arn` and `aws_session_token_env_var` (Airflow's AWS connection supports them); keep `resource_key` so a connection id like `aws_default` maps one to one.
- **`snowflake_resource`**: accept `account` and `user` as environment variables (secret hygiene); key-pair and PAT authentication as in `snowflake_stored_procedure_call_asset`.
- **`s3_cleanup_job`**: delete by an explicit key list or an upstream asset's output, not only by age.
- **`snowflake_stored_procedure_call_asset`**: add retry policy, pool and automation condition.

## New components for the operators the converter generates helpers for

Each mirrors an Airflow operator exactly and should ship with a parity test against that operator (hook mocked, recording client), as the converter's helpers did.

| Component | Mirrors | Notes |
|---|---|---|
| `s3_list_keys_asset` | `S3ListOperator` | prefix, delimiter; the paginate call Airflow's hook makes; returns the key list |
| `s3_copy_object_asset` | `S3CopyObjectOperator` | `s3://` URL or bucket plus key; `ACL=private` default |
| `s3_delete_objects_asset` | `S3DeleteObjectsOperator` | keys, chunks of 1000, raise on `Errors` |
| `s3_upload_file_asset` | `LocalFilesystemToS3Operator`, `S3Hook.load_file` | `replace`, existence check by HTTP 404, `Config=` default |
| `snowflake_sql_asset` | `SnowflakeOperator`, `SQLExecuteQueryOperator` | statements from text, list or file; multi-statement split; session overrides as `USE` statements; parameters |
| `snowflake_copy_into_asset` | `CopyFromExternalStageToSnowflakeOperator` | the exact COPY INTO template (whitespace included, so SQL can be compared); returns file counts as metadata |

## How to verify a new component

The harness in `examples/runtime_conversion/parity/` and `examples/s3_to_snowflake_factories/parity/` already does it: run the real Airflow operator or hook with the external system mocked and recording, run the Dagster component with an equivalent recording fake, compare the calls, return values and errors. Two lessons from building the fakes: Dagster resources attach derived objects (`S3Resource` attaches the boto client, `SnowflakeResource` a `SnowflakeConnection`), so fakes must attach the same; and a fake client has to include what Airflow's code reads (`ResponseMetadata`, `LastModified`) or the comparison fails for the wrong reason.
