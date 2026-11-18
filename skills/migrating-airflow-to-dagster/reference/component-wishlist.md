# What the components need to do (handoff to the components session)

The converter turns existing orchestrator DAGs into Dagster definitions. It owns the translation: it reads the DAG, works out what each part does, and writes either generated code or component configuration. This file lists what the components still need, in Dagster terms. None of it is specific to any orchestrator.

Measured on 292 DAGs from 14 public example repos; the converter maps 108 today. "Blocked" is the number of DAGs held back by a capability; "alone" the number held back by it and nothing else. The corpus is heavy on teaching repos, so use the ranking, not the percentages.

## The main message: most of what is left needs the converter to use existing components, not new ones

These blockers are covered by components you have already built. The converter has not been wired to them yet, so they are my work, not yours:

| Blocked DAGs (alone) | Capability | Existing component |
|---|---|---|
| setup/teardown 9 (0), `all_done_setup_success` 9 (0) | resource that wraps a scope | `scoped_setup_teardown` |
| `TriggerDagRunOperator` 8 (6) | launch another job and wait | `trigger_job_asset` (it always waits and uses `execute_job` out of process, so it fits `wait_for_completion=True`) |
| `ExternalTaskSensor` 5 (2) | react to another job finishing | `event_automation` run-status trigger |
| virtualenv and external-python callables 9 (1) | run code in its own interpreter | `pipes_python_asset` |
| task callbacks 10 (4) | failure and success hooks | `@on_hooks` (the converter writes the adapter) |
| delta schedules 8 (2) | a schedule that is not a plain cron | the schedule component |

## What I verified against Airflow's own operators (`examples/runtime_conversion/component_parity/`, components 0.10.161)

- **S3 copy, delete, list, upload:** 9 scenarios match, including delete's error response and 1,500 keys in chunks.
- **Snowflake `COPY INTO`:** 4 of 4 configurations match (the `ON_ERROR` quoting is fixed).
- **`shell_command_asset` output options:** `return_last_stdout_line` with `parse_json_output` matches Airflow 3 on 11 of 11 cases.
- **`af_guard` against the converter's gate:** the same decision about whether a step runs in all 672 combinations of upstream states; see request 1 for the one label difference.
- **`partition_window_sensor` window keys:** 12 of 12 cases match Airflow's `RollupMapper` and `FanOutMapper` as key sets (`examples/runtime_conversion/partition_window_parity/`).
- **Built but not yet tested by me:** `wait_until_asset`, `dynamic_fanout_asset` with `items_from`, `python_callable_asset` inputs read from upstream metadata, `s3_list_keys_asset` `return_keys_as_value`, the schedule component's date tags, `snowflake_sql_asset`, `k8s_job_asset` `use_pipes`.

## Requests

### 1. `af_guard` should label a step that did not run the way Airflow does

When a step does not run, `af_guard` always reports `skipped`. Airflow reports `upstream_failed` when a failure caused it (for example `all_success` or `none_failed` with a failed upstream) and `skipped` otherwise, and the label changes what later steps do: after a skipped step a `none_failed` step runs, after an upstream-failed step it does not. The converter's gate uses Airflow's own table (`TriggerRuleDep`); `examples/runtime_conversion/component_parity/gate_cross.py` lists every combination that differs, and `gate_cross_run.py` shows the run decisions already agree (0 of 672 differ). Offer an `upstream_failed` state in `StepOutcome` and set it per rule.

### 2. Decisions I need from you

- **Airflow partitioned timetables** (`CronPartitionTimetable`, `PartitionedAssetTimetable` with mappers; about 20 DAGs, all from one example file): the Dagster side is built (the schedule component's partitions and `partition_window_sensor`). The converter side is not started.
- **Airflow-only asset events** (`Metadata`, `outlet_events`, `inlet_events`, `AssetAlias`; about 12 DAGs): the script orchestrator names each outlet asset (`airflow_dataset_<cleaned name>`) and turns schedule inlets into dependencies, but it does not handle events or aliases either. The natural Dagster mapping is materialization metadata (what the converter already does for `xcom_push`), and a read of an asset's latest materialization metadata for `inlet_events`. Say if you want that built.

## Settings that components need to expose (small, mechanical)

An earlier version of this file said components lack automation conditions, partitions and metadata. That was wrong: any component's assets can take `automation_condition`, `partitions_def`, `metadata`, `kinds`, `owners`, `tags`, `group_name`, `key`, `deps`, `skippable`, `code_version` and `freshness_policy` through Dagster's `post_processing` in `defs.yaml` (checked in Dagster 1.13: `AssetAttributesModel`). What `post_processing` cannot set is anything on the execution side of an asset: the retry policy, the pool and a timeout. Those have to be attributes of the component. Checked in 0.10.162:

| Component | retry | pool | timeout |
|---|---|---|---|
| `python_callable_asset` | yes | no | yes |
| `shell_command_asset` | yes | yes | yes |
| `wait_until_asset` | yes | no | yes |
| `dynamic_fanout_asset` | yes | no | no |
| `s3_copy_object_asset`, `s3_delete_objects_asset`, `s3_list_keys_asset`, `s3_upload_file_asset` | no | no | no |
| `snowflake_sql_asset`, `snowflake_copy_into_asset` | no | no | no |
| `trigger_job_asset`, `pipes_python_asset`, `conditional_asset` | no | no | no |

Request: add `retry_policy_max_retries`, `retry_policy_delay_seconds`, `retry_policy_backoff`, `pool` and `timeout_seconds` (a shared set, as `shell_command_asset` already has) to every component in the "no" cells. With those, the converter can emit a component plus a `post_processing` block for everything else, instead of generating Python for the same work.

## New components that the converted DAGs would use

Provider operators seen in the public example DAGs with no component yet: an S3 "create object" step (`S3CreateObjectOperator`: bucket, key, data, replace), an S3 "list prefixes" step (`S3ListPrefixesOperator`; `s3_list_keys_asset` already returns `common_prefixes` as metadata, so this may only need a value option), and a SQL column check (`SQLColumnCheckOperator`, which maps to Dagster asset checks).

## Reading an asset's latest materialization metadata

Airflow 3's `inlet_events[asset][-1].extra` reads the metadata of an asset's latest event. `python_callable_asset` can read a named metadata value from an upstream materialization in the same run; add the same for the latest materialization of any asset (any run), with a default when there is none.

## Low priority

- `automation_condition` presets on `shell_command_asset` and `snowflake_stored_procedure_call_asset`.
- `shell_command_asset`'s description says it wraps `dagster-shell`, but the code uses `subprocess`.
- `k8s_job_asset` has no Pipes variant for streamed logs.
- `timeout_asset` abandons its thread at the deadline and the work continues; say so in the README.

## How each component gets checked

For each component, the harness runs the original operator with the external system mocked and recording, runs the Dagster component against an equivalent recording fake, and compares outcomes, calls and values. Remaining differences go in the migration report. The scripts are in `examples/runtime_conversion/component_parity/`. Dagster resources attach derived objects (`S3Resource` attaches the boto client, `SnowflakeResource` a `SnowflakeConnection`), so fakes must attach the same.
