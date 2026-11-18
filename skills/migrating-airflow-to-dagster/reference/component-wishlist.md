# Handoff to the components session: what the Airflow converter still needs

The Airflow-to-Dagster converter rewrites Airflow DAGs into Dagster-native definitions. It should emit your general components, not Airflow look-alikes, so nothing here asks for Airflow clones. Where behavior differs from Airflow, the converter records the difference in its report.

Measured on 292 DAGs from 14 public Airflow example repos. The converter maps 80 mechanically. "Blocked" counts DAGs with that blocker; "alone" counts DAGs whose only blocker it is (what one change unlocks by itself). The corpus is heavy on teaching repos, so use the ranking, not the percentages.

## Already done, per the components session (the converter has not run these yet)

`python_callable_asset`, `conditional_asset`, `trigger_job_asset`, `pipes_python_asset`, `scoped_setup_teardown`, the schedule component (`CronScheduleComponent`), six S3 and Snowflake mirror components, the S3 and Snowflake resource attributes, `s3_cleanup_job` key list, static partitions everywhere, and storage discovery through `s3_monitor`, `gcs_monitor`, `adls_monitor` and `filesystem_monitor`. I will run each against its Airflow counterpart and report anything that does not behave as expected.

## Requests, in priority order

### 1. Dynamic partition discovery from a callable, and from SQL (unlocks up to 24 DAGs alone)

Dynamic task mapping (`.expand`) is the largest blocker (31 DAGs, 24 alone).
- **New:** a component that calls a user function returning a list of items, registers each new item as a dynamic partition (`add_dynamic_partitions`), and requests one run per item with a content-keyed `run_key`. Same shape as `s3_monitor`, with the listing replaced by a callable. Include `keep_days` pruning.
- **Enhance `sql_monitor`:** it runs once per new row but does not register dynamic partitions. Add an option to do so, with the partition key taken from a named column.
- Acceptance: a test that runs the component twice with an overlapping item list and shows only the new items get runs and partitions.

### 2. Trigger rules (29 DAGs blocked, 2 alone, but it removes a blocker in many others)

`automation_condition_applicator` has four of five presets. Add `all_of_updated_since_last_run`: every listed upstream updated since the downstream last ran, written as the per-asset `asset_matches(...newly_updated()).since_last_handled()` AND, not a single `since_last_handled` over an AND (that form was wrong in my tests). A test with `dg.evaluate_automation_conditions` should show it does not fire when only one of two upstreams updated.

### 3. Dates for scheduled, un-partitioned DAGs (needed by every converted DAG that uses `ds` or `logical_date`)

Decision: a scheduled DAG with no natural time partition keeps a regular schedule and regular assets; no partitions are added.
- `CronScheduleComponent` with `add_logical_date_tag: true` already tags each run `logical_date` (un-partitioned: the trigger time).
- **`python_callable_asset` should read that run tag first**, then the partition window, then fail or warn. It currently falls back to `now()` silently, which is a behavior change that goes unnoticed.
- **Schedule option for Airflow 2 dates:** Airflow 2's logical date is the start of the interval that ended (for a cron, the previous tick). Add an option such as `logical_date: trigger | previous_tick` and `data_interval_start` / `data_interval_end` tags. Keep the tag names `logical_date`, `data_interval_start`, `data_interval_end` so the two components agree.
- Also expose `ts_nodash` and `execution_date` in the callable's context, since callable bodies are copied unchanged.
- Render any Jinja in a sandboxed environment (it currently uses a plain `jinja2.Environment`).

### 4. Generic callable sensor (`python_sensor`; sensors blocked 15, 8 alone)

A sensor that calls a function returning a bool (or a dict of run-request fields) and requests a run when it is true, with `minimum_interval_seconds` and a `run_key` option. Time, file and external-task waits are covered by the cron schedule, `*_monitor`, and `event_automation`, so this is the only sensor gap.

### 5. Low priority: smaller attribute gaps

Only worth doing if cheap. Pools are not on this list: only 2 of 292 corpus DAGs use a named pool, and the converter sets `pool=` directly on the assets it generates.
- `automation_condition` (named presets, the existing pattern) on `shell_command_asset` and `snowflake_stored_procedure_call_asset`, so asset-triggered DAGs can use them as components.
- `shell_command_asset`: its description says it wraps `dagster-shell`, but the code uses `subprocess`.
- `k8s_job_asset`: no Pipes variant (`PipesK8sClient`) for streamed logs.

### 6. Behavior that differs from Airflow, to document or fix

- `hooks_asset` (`@on_hooks`): hooks fire on every attempt, so a retried task fires `on_failure` several times. Airflow fires its failure callback after the last attempt only. Add a "final attempt only" option (`context.retry_number` against the retry policy's maximum). This blocks 10 DAGs that use task callbacks (5 alone).
- `timeout_asset` (`@timeout`): it abandons a worker thread at the deadline and the work keeps running, whereas Airflow stops the task. State this in the README, and where possible offer a variant that runs the callable in a subprocess that can be killed (`pipes_python_asset` may already do this).

## Decisions I need from you (not component work)

- **Branching and short-circuit** (27 DAGs blocked, 10 alone): `conditional_asset` exists. Confirm how downstream assets are skipped, because the converter must state the behavior exactly (blocking asset check, metadata-flag automation condition, or something else).
- **Airflow 3.2 partitioned timetables** (`CronPartitionTimetable`, `PartitionedAssetTimetable` with mappers; 10 DAGs, all from one Airflow example file): not started. It needs a mapping spec from Airflow's key formats and mappers to Dagster partition definitions and mappings. Low priority; Airflow 3.2 only.

## How the converter will use and check each component

For each component I run the real Airflow operator with the external system mocked and recording, run the Dagster component with an equivalent recording fake, and compare outcomes, calls and values; remaining differences go in the migration report. Two lessons from building the fakes: Dagster resources attach derived objects (`S3Resource` attaches the boto client, `SnowflakeResource` a `SnowflakeConnection`), so fakes must too; and a fake client needs what the library code reads (`ResponseMetadata`, `LastModified`). The harness is in `examples/runtime_conversion/parity/` and `examples/s3_to_snowflake_factories/parity/`.
