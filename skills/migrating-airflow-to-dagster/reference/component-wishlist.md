# Component and integration enhancements that would convert more Airflow DAGs

Measured on 292 DAGs from 14 public Airflow example repos (the corpus is heavy on teaching repos, so absolute counts overstate how rare these constructs are in production; use the ranking, not the percentages). The converter maps 80 DAGs mechanically. For each unconverted DAG it records every blocker; "alone" below counts DAGs whose only blocker is that category, so it is what one enhancement would unlock by itself.

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

Status from the components session (not yet verified by the converter): most of this exists.

- Static list: `partition_type: static` with `partition_values` is supported across the component set, including `python_callable_asset`. No new component needed; the converter should emit it for an `.expand` over a literal list.
- Discovery from object storage or the filesystem: `s3_monitor`, `gcs_monitor`, `adls_monitor` and `filesystem_monitor` list items, call `add_dynamic_partitions()`, and request one run per item with a content-keyed `run_key`. Two of them have a `keep_days` pruning option. This replaces the hand-written schedule in `examples/s3_to_snowflake_factories/example_04_per_file/` and is what the converter should emit for a mapped task over an S3 listing.
- Remaining gaps: (1) `sql_monitor` runs once per new row but does not register dynamic partitions (an enhancement, not a new component); (2) discovery from an arbitrary callable has no component.

Converter work: detect the mapping source (literal list, S3 prefix listing, SQL query, callable) and emit the matching component. Mapping over a runtime value with no listable source stays deferred.

## C. Automation-condition library (trigger rules)

Named, tested conditions that express Airflow trigger rules at the asset level: `all_upstream_done` (run after upstream succeed or fail), `any_upstream_failed`, `none_failed_min_one_success`, `all_of_updated_since_last_run` (the per-asset `asset_matches(...).since_last_handled()` AND used by the converter, verified), and `any_of_updated`. Expose them as strings in component YAML (`automation_condition: all_upstream_done`) and as Python helpers. Each needs a test using `dg.evaluate_automation_conditions` (see `examples/runtime_conversion/parity/and_trigger.py`).

## D. Conditional materialization (branch / short-circuit)

`conditional_asset`: a callable returning a bool (short-circuit) or the name of the branch to run (branch), and a way for downstream assets to be skipped rather than failed. Design needed: asset checks with `blocking=True`, or an automation condition keyed on a metadata flag. Without a decision here these DAGs stay hand-converted.

## E. Schedule components

`airflow_schedule`: cron or preset, timezone, `semantics: trigger | data_interval`, run tags carrying the logical date, optional `end_date`. Delta timetables (`timedelta`, `DeltaTriggerTimetable`) map to cron only when the interval divides the period; otherwise a custom schedule. `@once` as a manual-only job.

## F. Sensor components

Status from the components session (not yet verified by the converter):

- Time-of-day waits: a cron schedule (`cron_schedule`) already covers it. Nothing to build.
- File or key sensors: `filesystem_monitor` and `s3_monitor` cover the watch-and-fire half (`minimum_interval_seconds` is the poke interval). `event_automation`'s absence trigger covers "gave up waiting". The converter should state that Dagster sensors do not block a run the way a poking sensor task does: the downstream asset runs when the file arrives, so the DAG shape changes.
- `ExternalTaskSensor`: `event_automation` has a run-status trigger plus a launch-job action.
- Remaining gap: a generic callable-based sensor (`python_sensor`: a callable returns a bool, then a run request).

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

## Review of `dagster-community-components` 0.10.159: what the converter should emit

The components are general Dagster components, not Airflow clones, so the goal is not 1:1 behavior. The converter rewrites each Airflow construct into the Dagster-native form, and the parity harness checks that outcomes and values still match, with each remaining difference written down. Read from the installed package; nothing was executed yet.

`PythonCallableAssetComponent` (`callable_path`, `inputs`, `deps`, `params`, partition fields, `timeout_seconds`, `context_mode`) is the right target for a Python task: the function stays in the project, XCom becomes `inputs`, and dates come from the Dagster partition. How each Airflow idea should be rewritten:

| Airflow idea | Dagster-native rewrite | Note |
|---|---|---|
| `ds` / `logical_date` / `data_interval_*` | regular schedule and regular asset; the run carries the date as a tag | Decision: do not add partitions to a scheduled DAG that has none. `CronScheduleComponent` with `add_logical_date_tag: true` already tags each run `logical_date` (un-partitioned: the trigger time, which is Airflow 3's logical date). `PythonCallableAssetComponent` does not read that tag yet (it uses the partition window, else `now()`), so the pairing needs one change there: read the `logical_date` run tag first. Airflow 2's interval-start date for an un-partitioned schedule is not offered by either component (see below). |
| `ti.xcom_pull(task_ids=...)` | `inputs: {name: asset_key}` | Native. |
| `Variable.get` | environment variable or a Dagster resource | `AIRFLOW_VAR_*` is a migration convenience, not the target. |
| `S3Hook` calls | the S3 mirror components or `S3Resource` | Rewrite the call site; do not carry a hook shim. |
| Jinja in operator arguments | resolve at definition time where static; otherwise partition-derived values | Avoid a runtime template engine in the final project. |

Gaps to close for the un-partitioned path: (1) `PythonCallableAssetComponent` reads the `logical_date` tag set by `CronScheduleComponent`; (2) an option on the schedule component for Airflow 2's date (the start of the interval that ended, which for a cron is the previous tick), such as `logical_date_offset: previous_tick`, with `data_interval_start/end` tags; (3) the tag name stays `logical_date` so the two components agree.

Where the component should improve, judged as a Dagster component (not as Airflow): render any Jinja it keeps in a sandbox (it uses a plain environment today), fail at load time rather than at render time for unsupported names (it already does this for the whitelisted roots), and say what happens for a non-partitioned asset instead of falling back to `now()`.

What this changes for the converter: the generated runtime module (`runtime.py`, with its Airflow-shaped context, hook shim and Jinja) is a fallback for DAGs that cannot yet be rewritten natively. The preferred output is the native form above, with parity checked on outcomes and values.

## Registry decorators that map deferred Airflow settings

The registry has a `decorator` category (`dagster-component search timeout`). Candidates, each to be verified against Airflow before it is used:

| Airflow setting | Decorator | Differences to handle |
|---|---|---|
| `on_failure_callback`, `on_success_callback` | `hooks_asset` (`@on_hooks`) | Airflow passes one context dict, the hooks take `(context, exc)` (the converter would generate an adapter). Hooks wrap each attempt and fire on every retry, while Airflow fires the failure callback only after the last attempt. A "final attempt only" mode would help. |
| `execution_timeout` on a non-shell task | `timeout_asset` (`@timeout`) | It abandons a worker thread at the deadline and the work keeps running, whereas Airflow stops the task. Shell tasks already get a real kill from the converter's runtime. |
| `sla` | `sla_asset` (`@sla`) | Both record the breach and do not kill the task. |
| `pool`, `max_active_tis_per_dag` | `rpa_queue_concurrency_lock`, or Dagster pools | Native pools are preferred; check the lock component first. |

## Settings the converter defers that a feature could map

These are candidates, not verified mappings; verify each against Airflow's behavior before relying on it.

- Task and DAG concurrency (`max_active_runs`, `max_active_tasks`, `max_active_tis_per_dag`): a run-queue tag concurrency limit is deployment configuration. A converter could tag the job and emit the limit for the deployment; a component could hold the tag.
- Task callbacks (`on_failure_callback`, `on_success_callback`): run-status or run-failure sensors, or alert policies.
- `dagrun_timeout`: a run time limit tag, if the deployment enforces one.
- `fail_stop=True`: no direct equivalent; needs a design decision (terminate the run from a failure hook).
