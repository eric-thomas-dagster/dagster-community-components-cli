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

## What I verified against Airflow's own operators (`examples/runtime_conversion/component_parity/`)

- **S3 copy, delete, list, upload (components 0.10.160):** 9 scenarios match, including delete's error response and 1,500 keys in chunks.
- **Snowflake `COPY INTO`:** 4 configurations match after normalizing whitespace, with one textual difference: the component writes `ON_ERROR='CONTINUE'` (quoted) where Airflow passes `ON_ERROR = CONTINUE`. Please confirm Snowflake accepts the quoted form.
- **Not yet tested:** the schedule, callable, sensor and conditional components, and `snowflake_sql_asset`.

## Requests

### 1. Return values where downstream steps read them (small, high value)

- `s3_list_keys_asset` reports the key list only as `keys` metadata. Add an option to return it as the asset's value, so a downstream asset can take it as an input the usual way.
- `shell_command_asset` returns nothing. A shell step's last line of output is what downstream steps read in the source system. Add an option (off by default) to return the last stdout line as the asset's value, with a second option to parse it as JSON when it is valid JSON (the converter does both today in its own runtime helper, and the JSON behavior depends on the source system's version).

### 2. Read a named value from an upstream materialization (small)

A task can push several named values and a later task reads them by name. The converter implements this today as metadata on the pushing asset's materialization and a lookup in the same run. Add the same capability to the callable component: an input mapping `{argument: "upstream_asset_key.metadata_key"}` that reads the named metadata value from the upstream asset's materialization in the current run, with a default when it is missing.

### 3. A reusable step gate (the converter generates about 150 lines for this today)

For a pipeline expressed as one graph asset, each step needs to decide, from the states of the steps before it, whether to run. The cases: a branch names the steps to follow; a short-circuit skips everything after it, or only the next step; a join that runs when some upstreams were skipped; a cleanup step that runs after a failure; an alert that runs only when something failed. `conditional_asset` covers skipping but cannot rejoin when only one of several branches ran, and a Dagster step is skipped if any input was skipped, so a native join cannot do it. What the converter generates (`af_gate`, `af_guard` and `af_finish` in its runtime): a step returns a marker (skipped, failed, upstream-failed) instead of raising, the next step evaluates a named rule over its inputs' states, and a final step fails the run if anything failed. Offering this as a decorator pair (a gate on a step, a guard that turns a failure into a value) would let the converter emit it instead of carrying its own copy, and would make `conditional_asset` rejoin-capable.

### 4. A blocking wait step (the converter generates the helpers today)

Many pipelines hold a step open until a condition holds, with a polling interval and a timeout, and then either fail or skip on timeout. The conditions: a callable returns true; a shell command exits 0 (with an optional "try again" exit code); a file matching a pattern exists; a time of day has passed; a delay after the run's reference time has passed. The existing `python_sensor`, `*_monitor` and `event_automation` components start runs; they do not hold a step open, and the pipeline shape differs. A component or decorator `wait_until` with those condition types, `poll_seconds`, `timeout_seconds` and `on_timeout: fail | skip` would replace the converter's helpers.

### 5. Run a callable once per item inside one run (the converter generates a graph for this today)

For "do this for each item the previous step returned, then use all the results", the converter builds a graph asset with a fan-out step, a mapped step and a collect step. A component taking `items_from` (an upstream asset or a literal list), a `callable_path`, per-item arguments, and an option to collect the results would let the converter emit configuration. This is separate from the existing per-item partition components, which make each item its own run.

### 6. Decisions I need from you

- **Branch and skip behavior of `conditional_asset`:** if request 3 is not built, say plainly in its README that it cannot rejoin, so the converter documents that.
- **Airflow 3.2 partitioned timetables** (`CronPartitionTimetable`, `PartitionedAssetTimetable` with mappers; 10 DAGs, all from one example file): needs a mapping from key formats and mappers to Dagster partition definitions and mappings. Not started. Low priority; it only exists from Airflow 3.2.

## Low priority

- `automation_condition` presets on `shell_command_asset` and `snowflake_stored_procedure_call_asset`.
- `shell_command_asset`'s description says it wraps `dagster-shell`, but the code uses `subprocess`.
- `k8s_job_asset` has no Pipes variant for streamed logs.
- `timeout_asset` abandons its thread at the deadline and the work continues; say so in the README.

## How each component gets checked

For each component, the harness runs the original operator with the external system mocked and recording, runs the Dagster component against an equivalent recording fake, and compares outcomes, calls and values. Remaining differences go in the migration report. The scripts are in `examples/runtime_conversion/component_parity/`. Dagster resources attach derived objects (`S3Resource` attaches the boto client, `SnowflakeResource` a `SnowflakeConnection`), so fakes must attach the same.
