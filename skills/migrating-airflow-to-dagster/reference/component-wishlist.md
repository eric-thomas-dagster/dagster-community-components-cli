# What the components need to do (handoff to the components session)

The converter turns existing orchestrator DAGs into Dagster definitions built from your components. It owns the translation (it reads the DAG, works out what each part is doing, and writes the component configuration). The components only need the capabilities below. None of them is specific to any orchestrator; each is a general Dagster capability that the converter happens to need.

Measured on 292 DAGs from 14 public example repos; the converter maps 80 today. "Blocked" is the number of DAGs held back by that capability; "alone" is the number held back by it and nothing else. The corpus is heavy on teaching repos, so use the ranking, not the percentages.

Tested so far against Airflow's own operators (`examples/runtime_conversion/component_parity/`, components 0.10.160): the S3 copy, delete, list and upload components match on 9 scenarios, and the Snowflake `COPY INTO` component matches on 4 configurations apart from one quoting difference (`ON_ERROR='CONTINUE'` against Airflow's unquoted form; please confirm Snowflake accepts it). Not yet tested: the schedule, callable, sensor and conditional components and `snowflake_sql_asset`.

Already built, per the components session: the Python callable, conditional, trigger-job, Pipes-Python and setup/teardown components, the schedule component, six S3 and Snowflake components, static partitions, and storage discovery through the `*_monitor` components.

## 1. Discover items, then run once per item (blocked 31, alone 24)

Many pipelines process "whatever exists today": files under a prefix, rows in a table, ids from an API. The converter's default for mapped tasks is dynamic outputs inside one run; this item is for the case where each item should be its own run and partition (one file, one customer), which is what the converter's `partitions` mode emits.
- A component that calls a user function returning a list of items, registers each new item as a dynamic partition, and requests one run per item with a key that makes the request idempotent. Same shape as `s3_monitor`, with the listing replaced by a function. Include pruning of old partitions.
- `sql_monitor` should be able to do the same: register a dynamic partition per new row, with the partition key taken from a named column.
- Test: run twice with an overlapping list; only the new items get partitions and runs.

Converter: finds the loop or mapping over a listing, identifies the source (literal list, storage prefix, query, function), and emits the matching component. A mapping over a value with no listable source stays unconverted, with the reason.

## 2. "Run when all of these have updated" (blocked 29, alone 2; it removes a blocker in many other DAGs)

`automation_condition_applicator` has four presets. Add one for "every listed upstream asset has updated since this asset last ran", built as an AND of per-asset `asset_matches(...newly_updated()).since_last_handled()` terms (a single `since_last_handled` over an AND gave wrong results in my tests). Test with `dg.evaluate_automation_conditions`: it must not fire when only one of two upstreams has updated.

Converter: a task that waits for several upstreams, or a pipeline triggered by several assets.

## 3. Code that needs to know the run's date (needed by every converted pipeline that uses dates)

Pipelines pass the run's date into queries, file names and filters. Without a time partition, Dagster has no such value, so the schedule and the code need a shared convention. Decision: a scheduled pipeline with no natural time partition keeps a regular schedule and regular assets; no partitions are added.
- The schedule component already tags each run with its scheduled time (`add_logical_date_tag`). Keep that, and add an option to also tag the start and end of the interval the run covers, since some pipelines process the interval that just ended.
- The callable component should read those tags first, then the partition window, and otherwise fail or warn. Today it falls back to `now()` without saying so, which silently changes behavior.
- Render templated arguments in a sandboxed environment, and make unsupported names fail when the project loads, not when the run starts.

Converter: finds each use of a date in the code and templates, and writes the matching component attributes. The converter's own runtime module remains as a fallback until the components do this.

## 4. A sensor driven by a function (blocked 15, alone 8)

A sensor that calls a function returning true/false (or a dict of run-request fields) and requests a run when it is true, with a polling interval and a key for idempotence. Time-based waits, file arrival and "another job finished" are already covered by the schedule component, the `*_monitor` components and `event_automation`.

Converter: a wait-for-condition step. It will say that a Dagster sensor starts the run when the condition holds; it does not hold a run open waiting, so the shape of the pipeline changes.

## 5. Failure and timeout behavior: covered by the existing decorators

`@on_hooks` and `@timeout` (both in the installed package) are applied by the converter as decorators on the assets it generates, so no component change is needed.
- `@on_hooks(on_failure=["pkg.hooks:fn"])` takes `module:function` references. The converter writes the callback module and an adapter that has the hook's `(context, exc)` signature. Hooks fire on every attempt, so the adapter itself checks `context.retry_number` against the task's retry count and acts only on the final attempt. No change to `@on_hooks` is required.
- `@timeout(seconds)` abandons a worker thread at the deadline and the work keeps running. For shell tasks the converter's runtime already kills the subprocess. For Python code, the converter will use `@timeout` and state in its report that the work is not stopped, only abandoned. Only a documentation change is asked of the components: say this in the README. A killable variant (run the callable in a subprocess, perhaps through `pipes_python_asset`) would remove the caveat but is optional.

Converter: task-level failure and success callbacks, and execution timeouts on Python code.

## 6. Decision needed from you: conditional execution (blocked 27, alone 10)

`conditional_asset` exists. Please state exactly what happens to downstream assets when a branch or check says "do not continue": skipped, not materialized, blocked by an asset check, or something else. The converter has to describe that behavior in its report, and it must hold when several branches rejoin.

## 7. Low priority

`automation_condition` presets on `shell_command_asset` and `snowflake_stored_procedure_call_asset`; `shell_command_asset`'s description says it wraps `dagster-shell` but the code uses `subprocess`; `k8s_job_asset` has no Pipes variant for streamed logs.

## How each component gets checked

For each component the converter's harness runs the original operator or code with the external system mocked and recording, runs the Dagster component with an equivalent recording fake, and compares outcomes, calls and values. Remaining differences go in the migration report. Dagster resources attach derived objects (`S3Resource` attaches the boto client, `SnowflakeResource` a `SnowflakeConnection`), so fakes must attach the same, and a fake client needs whatever fields the library code reads. The harness is in `examples/runtime_conversion/parity/` and `examples/s3_to_snowflake_factories/parity/`.
