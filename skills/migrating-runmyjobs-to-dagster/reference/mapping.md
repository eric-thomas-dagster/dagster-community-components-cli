# RunMyJobs to Dagster mapping

What each Dagster target does, and how to set it up, is in `dagster-targets.md`.

Class: MECH = converter output expected correct; JUDG = confirm or fix; REDESIGN = needs a design decision; NONE = no automatic translation.

| RunMyJobs | Dagster | Class | Notes |
|---|---|---|---|
| Process definition, OS | `ShellCommandAssetComponent` (components output) or asset + subprocess | MECH | Parameters become `$name` substitutions and `RMJ_PARAM_*` env vars. `run_as` is not enforced (Dagster runs as the code-location user). |
| Process definition, Python | script file run by `ShellCommandAssetComponent`, or asset + subprocess | MECH | Job type `Python` (named in the RunMyJobs MCP docs). Parameters become `RMJ_PARAM_*` env vars. |
| Process definition, SQL | `SqlAssetComponent` (components output) or asset + SQLAlchemy | MECH | Components output needs a resource named after the connection (`postgres_resource` and similar); the python output needs a connection env var. A SQL step with a pool, retry or timeout stays generated Python (the SQL component has none of those). Only the shell path has been run against a real command. |
| Process definition, RedwoodScript | loud stub asset (`RMJ-CAVEAT`), source in comments | JUDG (stub) | Port by hand or with the (planned) LLM pass. The stub fails until ported. |
| Process definition, connector types (SAP, others) | loud stub | NONE (stub) | Needs a dedicated integration. |
| Chain: steps run in order, calls in a step run in parallel | assets with deps from every call in the previous step; one job per chain | MECH | Calls from a step with no calls inherit the prior step's deps. |
| Nested chain call | loud stub in the chain's place (order and downstream deps kept) | JUDG (stub) | Flatten into the parent or run as a separate job. |
| Submit frame element: interval, daily, weekly, monthly, cron | `ScheduleDefinition`, one per element | MECH | Multiple elements are a union of cycles; one schedule each. |
| Submit frame time window | hour range in cron | JUDG | Minute-level windows are rounded to hours. |
| "Last day of month" | none (needs a custom schedule function) | JUDG | |
| Frame end date | `CronScheduleComponent` `end_date` (components output); not enforced in python output | MECH / JUDG | Python output lists it as a caveat; use components output. |
| Queue | pool on each asset | MECH | The limit must be configured in Dagster; the report lists each pool with the `dagster instance concurrency set` command and the run-monitoring setting that frees slots. See `dagster-targets.md`. |
| Maximum Starts | `RetryPolicy(max_retries=n-1)` | JUDG | Interpretation as total attempts is unconfirmed. |
| Status handler: restart step on error | `RetryPolicy` on the step's assets | MECH | |
| Other status handlers | none (not parsed yet) | JUDG | `EventAutomationComponent` (`run_status` trigger with `launch_job`, `retry_run` or notify actions) or `@run_failure_sensor` / `@run_status_sensor`; see `dagster-targets.md`. |
| Step or chain precondition (time window, REL expression) | none | JUDG | Candidates: schedule window, sensor, skip check inside the asset. |
| Events (file, raised) | `FilesystemMonitorSensorComponent` for files; `EventAutomationComponent` (`emit_event` + `asset_materialized`) for raised events | not built | File events have poll interval, minimum size and age; the sensor checks them. See `dagster-targets.md`. |
| Named calendars, nested time windows | schedule filter | not built | |
| Process outputs (tables, files) | `metadata.produces` | not built | Real lineage needs external assets and table-level deps. |

## What the rewrite route does with the constructs the converter stubs or degrades

The second route (`SKILL.md`, "Two routes") writes these by hand from the spec. Status says what was actually run (the worked units are in `examples/rewrite/`); everything else is not run.

| Construct (converter today) | Rewrite route | Status |
|---|---|---|
| RedwoodScript process (loud stub) | ported to a Python asset; the header says `reimplemented` and names what was dropped (a RunMyJobs API lookup such as `getJobByName` has no Dagster counterpart). The stub gate fails while any `RMJ-CAVEAT` remains | run on a synthetic unit (`sample`, `CALC_MARGIN`): ported, gates pass. The source is synthetic, so this says nothing about real RedwoodScript volume |
| "Last day of month" submit-frame element (dropped, degraded) | a `@dg.schedule` function on `0 23 28-31 * *` that only requests a run when tomorrow is the 1st | run on `PURGE_TEMP` (synthetic): gate checks the schedule exists with an explicit time zone; the day logic itself is not tested by the gates |
| Step or call precondition with a time window not in the export (not enforced) | stays unenforced and says so (`dropped: ... needs the hours`); with the hours it would be a `run_window` | run: both routes leave it unenforced; the rewrite cannot know the window either |
| Step status handler: restart | retry policy on the asset that holds the call (the gate checks the number) | run |
| Notification step that only echoes (generated shell step) | a run-status sensor that logs (a real email target would use `EmailSendAssetComponent` or an `EventAutomationComponent` email action) | run on `sample` (`notify`); not materialized by the gate (a sensor) |
| Chain with parallel calls (one asset per call) or a linear chain (one task-chain asset) | assets with the same order, folded or split as the spec's data products suggest; the gate checks every earlier-to-later pair | run |
| Queue shared by several chains | one pool, one limit; the manifest queue unit completes only when every unit using it has passed | run on `linear_chain` (two chains, one queue) |
| SAP call with no job name or process chain in the export | cannot be ported: the rewrite stays a loud stub and fails the gate | run on `sample` (`LEGACY_SAP_EXTRACT`): deferred |
| Chains that call chains (inlined by the converter) | an estate decision (one pipeline, or parent over separate sub-pipelines), made from `ESTATE.md` | the estate view is produced and tested; only unit-level rewrites were written, the estate prompt was not followed |

## Confirmed from public documentation

(via search summaries of documentation.runmyjobs.cloud and docs.cs.redwood.com; the sites block automated fetches)

- Steps in a chain run in order; calls within a step run concurrently.
- Preconditions (time window and/or REL expression) are evaluated each time a step or chain runs, including restarts.
- Restart behavior is evaluated at the end of a step; status handlers can restart a step or the chain.
- Submit frames contain one or more elements; several elements form a union of cycles. Elements can be limited by a time window; frames can have an end date and a time zone.
- A time window is open when at least one of its elements is open; elements include their opening time but not their closing time; windows can include or exclude other windows.
- Raise-event and wait-event definitions can be gated by a time window. File events raise a parent event with poll interval, minimum size, minimum age and a file-in-use check.
- Objects export as XML inside CAR archives (export rule sets, "export with related objects"). Object type names seen: `JobDefinition`, `JobChain`. A community report says exports do not carry schedules.
- Inbound REST Extension: path of the form `/api-extension/External/REDWOOD/Redwood_RestService/rest/v1/JobDefinition/<partition>.<name>` (older version page); auth by Basic, Bearer token or API key.

## Verified against a real export sample

From a real `JobDefinition` XML (build 2023.2) and Redwood's description of the CAR layout:

- A CAR is a directory or zip with `META-INF/export-info.xml` (source-system, source-address, user, date) and one directory per object type holding `<partition>.<name>.xml` files. Folders are called Applications.
- `JobDefinition` elements used by the extractor: `Name`, `Partition@path`, `ParentApplication@path`, `JobDefinitionType@path` (e.g. `GLOBAL.RedwoodScript`), `DefaultQueue@path`, `Description` (may contain MadCap tags), `JobDefinitionParameter` (`Name`, `DataType`, `DefaultExpression`, `Direction`, `Nullable`), and `Script/Source`, `Script/RunAsUser`.
- Fields present but not yet mapped (flagged when set): `TimeWindow`, `Resource`, `RestartCount`, `ReturncodeMapToCompleted`, `JobTimeZone`, `HoldOnSubmit`, `CriticalProcess`.

## Unverified (check before relying)

- XML for chain definitions (steps, calls), queues (limits), time windows, events, and submit frames: no sample seen yet.
- REST endpoints for listing definitions. The `runmyjobs_integration` component's `/scheduler/api/...` paths do not match the documented Inbound REST path and were only tested against a mock.
- Exact meaning of "Maximum Starts".
- RedwoodScript API surface.

## Facts from the RunMyJobs MCP docs (supplied by the user)

- Job definition types named: RedwoodScript, Python, BASH. Business keys are `PARTITION.NAME`.
- `get_job_definition` returns the definition and its parameter schema (`JobDefinitionParameters`, parameter names from the `Name` field).
- Queues link to process servers through queue providers (`executionSize`, `held`, `holdLevel`, `inclusive`, `timeWindow`); `executionSize` as a concurrency limit is a hypothesis.
- Event definitions can be read and updated; events are raised and cleared by business key.
- No tool for listing chains, schedules or time windows appears in the catalog.
