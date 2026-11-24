# What the components could do for the RunMyJobs converter

For the components session. The converter turns a RunMyJobs installation into Dagster definitions; `--output components` writes registry-component YAML for everything a component covers. This file lists what is missing, in Dagster terms (nothing here is specific to one scheduler), ranked by how many converted objects it would remove from generated Python or from the "needs review" list. Check the registry first: the last section lists what already exists.

## What is used today (checked: loads, matches the manifest, a shell asset ran; shell and SQL through the converter's dry-run subclasses)

| RunMyJobs | Component | Fields used |
|---|---|---|
| Process definition, OS | `ShellCommandAssetComponent` | `command`, `env_vars`, `timeout_seconds`, `pool`, `retry_policy_*`, `deps`, `group_name`, `description`, `asset_tags`, `kinds` |
| Process definition, Python | `ShellCommandAssetComponent` running a generated script | same (the Python component has no `pool`) |
| Process definition, SQL | `SqlAssetComponent` | `asset_key`, `resource_key`, `sql`, `deps`, `pool`, `retry_policy_*`, `timeout_seconds`, `run_window`; the converter generates the `RmjConnection` resource it needs |
| Linear chain of shell/SQL/Python steps | `TaskChainAssetComponent` | `steps`, `pool`, `retry_policy_*`, `timeout_seconds` (sum), `run_window` |
| Submit frame element | `CronScheduleComponent` | `cron_expression`, `asset_keys`, `job_name`, `execution_timezone`, `end_date`, `default_status` |
| Reconciliation during parallel run | `runmyjobs_integration` | not generated yet; the parity step points at it |

## Requests

**Status: all six were delivered in registry v0.10.173 and the converter now uses them (installed and checked: gates 1-4 pass on a generated chain).** `TaskChainAssetComponent` carries a linear shell/SQL/Python chain whose queue, retries and window agree; SQL steps take `pool`, retry and timeout; `success_exit_codes` comes from the return-code mapping (parsed from `ReturncodeMapToCompleted`; the real value format is unverified); `run_window` is emitted from the model (no extractor reads RunMyJobs time windows yet). File events are done: a `file_events:` list in the model becomes `FilesystemMonitorSensorComponent` with `min_age_seconds`, `min_size_bytes` and `require_stable_size` (run against real files: only a stable, old-enough, matching file fired, once). No extractor reads RunMyJobs file events yet (the shape of the REST `EventDefinition` / CAR event is unknown), so they come from the hand-written model. Still converter-side and not done: moving the generated `Rmj*` dry-run subclasses to upstream `dry_run`, which the gate still needs for SQL (no `dry_run` on `SqlAssetComponent`) and for the `RMJ_DRY_RUN` env switch.

### 1. One component for a whole set of jobs (delivered: `TaskChainAssetComponent`, one asset per chain, stop on first failure, `run_window` clean skip)

A chain of 40 calls is 40 component documents today, plus one per schedule. Ask: a component that declares **an ordered set of command, SQL or Python tasks in a single document** and produces the assets, their dependencies, the job and the schedule.

- `steps:` as an ordered list; the tasks inside one step run in parallel and each step depends on all the tasks of the previous one (a plain "stages" shape, not RunMyJobs-specific; Airflow task groups and Control-M folders look the same).
- Per-task: `command` | `sql` | `script`, `parameters` (substituted into the text and exported as env vars), `pool`, `retry_policy_*`, `timeout_seconds`, optional `depends_on` for non-linear cases.
- Shared defaults at the top: `group_name`, `pool`, `retry_policy_*`, `execution_timezone`, `tags`, `owners`.
- An optional `schedule:` block (cron, timezone, `end_date`, `default_status`) so one document is one migrated chain.
- It can reuse `ShellCommandAssetComponent` and `SqlAssetComponent` internally; the point is the document count and one place for shared settings.

### 2. `pool`, retry and timeout on `SqlAssetComponent` and `PythonCallableAssetComponent` (delivered)

`SqlAssetComponent` has none of the three; `PythonCallableAssetComponent` has retry but no `pool` and documents `timeout_seconds` as unused. RunMyJobs queues and Maximum Starts apply to every process type, so a queue-bound SQL step falls back to generated Python today.

### 3. Run window: skip, do not fail, outside a window (delivered on shell, SQL and the task chain)

RunMyJobs time windows gate submit-frame elements and step or chain preconditions. Today they become a cron hour range (hours only) or a "not enforced" caveat. Ask: a `run_window` on shell, SQL and the task set (hours and weekdays, optional excluded dates or a named calendar) that records the run as **skipped** when outside the window.

### 4. Shell exit-code mapping (delivered: `success_exit_codes`)

RunMyJobs maps return codes to Completed (`ReturncodeMapToCompleted`). `ShellCommandAssetComponent` has `skip_on_exit_code` (one code, skip). Ask: `success_exit_codes: [0, 3]` (treated as success), and keep `skip_on_exit_code` as it is.

### 5. File arrival with a stability check (delivered: `min_age_seconds`, `min_size_bytes`, `require_stable_size`)

RunMyJobs file events have a poll interval, a minimum size, a minimum age and a "file in use" check. `FilesystemMonitorSensorComponent` has pattern, directory and interval. Ask: `min_size_bytes`, `min_age_seconds` (file unchanged for that long) and an in-use check, so an event converts without custom sensor code.

### 6. Make the registry components subclass-friendly, and own the dry run (delivered: subclass crash fixed, `dry_run` on shell)

The converter's dry-run gate needs the shell and SQL components to render and log instead of executing. It does that today with generated subclasses (`Rmj*` in the project's `components.py`, adding `dry_run` or `RMJ_DRY_RUN=1`), which works. Two things would make it cleaner:

- **Subclassing the exported names fails.** `dagster_community_components.ShellCommandAssetComponent` is a lazy placeholder: a subclass inherits `get_model_cls`, which looks the class up by its own name in the registry (`Unknown component: 'RmjShellCommandAssetComponent'`). The generated code works around it with the private `_load_real_class(name)`. Ask: export the real classes, or make the placeholder's lookup use the defining class's registered name, so `class My(ShellCommandAssetComponent)` just works.
- **`dry_run` upstream (optional).** A `dry_run` attribute (and an environment variable) on the shell and SQL components would remove the generated subclasses. Until then the subclasses are enough.

## Already in the registry: converter work, not component work

`EventAutomationComponent` (`sensors/event_automation`: 35 triggers, 27 actions in one YAML document) covers three things I first listed as requests. The converter does not generate it yet, because the extractor does not parse status handlers, events or pool limits from a real export (no samples). When it does:

| RunMyJobs | `EventAutomationComponent` | Note |
|---|---|---|
| Status handler: start another chain, notify, restart | trigger `run_status` (with `monitored_jobs`) with action `launch_job`, `retry_run`, `slack`, `email`, `pagerduty` and others | Check it covers "restart one step" (retry is already `RetryPolicy`). |
| Raise-event / wait-event (a named flag) | action `emit_event` in one job, trigger `asset_materialized` in the waiting one | Confirm clear-after-use semantics. |
| Chain max run time, overdue | trigger `run_duration`, `run_stuck`, `freshness_violation` | |
| Queue limits applied with the project | action `set_concurrency_limit`, `free_concurrency_slots` | Needs a trigger that fires once at load (a `schedule` trigger?). If there is none, the request is a "run once at startup" trigger. The report prints the `concurrency set` commands meanwhile. |

## Asset-shaped steps the converter now recognises (from a real job listing)

The converter recognises steps that call `JD_dbt_Cloud_Job_Trigger` (job id from the call parameters) and `JD_HVR_Refresh_CMD` / `JD_LDP_Refresh_CMD` (channel from the parameters), plus command text that calls the dbt Cloud API or `hvrrefresh`. Both are heuristics, never seen in a real definition. Two requests follow:

| Capability | Registry today | Request |
|---|---|---|
| Run one dbt Cloud job as an **asset** in a chain (wait for it, fail with the run URL) | `EnrichedDbtCloudWorkspaceComponent` with `mirror_jobs: executable` (in the registry before 0.10.173): each Cloud job is a materializable asset keyed by its sanitized job name, with the job id in its metadata; `deps` come from `post_processing`. **Done in the converter:** `fetch-dbt-jobs` lists the account's jobs, and `convert --dbt-jobs` maps each step's job id to the asset key, emits `mirror_jobs: executable` limited to those ids, and attaches the previous steps with `post_processing` `deps` (checked: the block applies and gate 3 matches the deps). A job id missing from the list keeps a generated Python trigger step. **Not verified:** the real component against a live account (nobody has one yet); the key rule is copied from the component's `_sanitize_job_name`, so if it changes the gate 3 check reports the mirrored asset as missing. Open for the component: confirm per-model assets can be switched off so a job is priced as one asset. |
| Refresh **one** HVR channel as an asset, with `deps` | `HvrHubWorkspaceComponent` (`action: refresh`) discovers every channel, so a chain step cannot name the asset it depends on | An `hvr_refresh_asset` (hub, channel, wait, timeout), or a documented asset-key scheme for the workspace component. The converter keeps the original command meanwhile |

## Not asking for

Human-approval steps, RedwoodScript translation (a converter or LLM-pass task), and SAP or other connector job types (each needs its own integration, ranked by what the customer uses).

## Asked for by real chains (three exports checked): delivered in registry 0.10.176 and 0.10.177

The three asks below were delivered in 0.10.176 and the converter uses them (Plan-to-Produce, Forecast_Replenishment and ISU Core all pass gates 1-4 with no stubs in components output).

| RunMyJobs | Component now used | Left |
|---|---|---|
| `SAP_AbapRun` (JOBNAME, ABAP_PROGRAM_NAME), `SAP_BW_ProcessChain` (NAME) | `SapJobTriggerAssetComponent` (`background_job` / `process_chain`, polls, `dry_run`, `run_window`, `pool`) | Never run against SAP: the README says so too. The BW `CLIENT` parameter belongs in the connection. |
| `System_Mail_Send` (To, Subject, Text) | `EmailSendAssetComponent` over `smtp_resource` | `JobFile` (attachment of an upstream log) is not converted. A recipient the chain supplies at run time keeps a generated step that takes it as run config. |
| SQL step without a registered resource | `SqlAssetComponent` / task-chain sql step `connection_env` and `dry_run` | none: the generated `RmjConnection` resource is gone |

**Fixed in 0.10.177:** `SapRFCResourceComponent` `client` ("100") and `sysnr` ("00") used to be turned into integers by Dagster's YAML resolver, so the component's own example did not load. Checked: they load from YAML now, and the converter writes the resource as YAML. The same coercion still affects any other string-typed field the converter fills (`env_vars`, `asset_tags`), which it works around.

**For the components session: `SapJobTriggerAssetComponent` (0.10.177) against SAP's XBP documentation.** Checked against SAP's XBP 6.10 interface description (the only XBP spec found online; XBP 2.0 has been stable since, but confirm on a current system). `tests/xbp_fake.py` is a strict fake written from it, and `tests/test_sap_xbp.py` runs the converter's generated component against it. The component fails at the first call today:

| Component calls | Documentation says |
|---|---|
| `BAPI_XMI_LOGON` with INTERFACE and VERSION only | `EXTCOMPANY` and `EXTPRODUCT` are required (not optional) |
| every XBP call without `EXTERNAL_USER_NAME` | required on JOB_OPEN, ADD_ABAP_STEP, CLOSE, START_IMMEDIATELY |
| `BAPI_XBP_JOB_ADD_STEP` with PROGRAM, VARIANT | no such function; `BAPI_XBP_JOB_ADD_ABAP_STEP` with `ABAP_PROGRAM_NAME`, `ABAP_VARIANT_NAME` |
| `BAPI_XBP_JOB_CLOSE` with `START_IMMEDIATELY="X"` | CLOSE has no such parameter; start with a separate `BAPI_XBP_JOB_START_IMMEDIATELY` |
| `BAPI_XBP_JOB_START_IMMEDIATE` | the function is `BAPI_XBP_JOB_START_IMMEDIATELY` |
| `BAPI_XBP_JOB_READ_JOBLOG` | the function is `BAPI_XBP_JOB_JOBLOG_READ` |
| `success_statuses` FINISHED/COMPLETED/GREEN, `failure_statuses` ABORTED/... | `STATUS` is one character: R active, I intercepted, Y ready, P scheduled, S released, A cancelled, F finished. As shipped, no poll matches, so a job that finished still times out. The converter now passes `F` and `A` itself; the defaults should change |

The BW process chain functions (`RSPC_API_*`) are not in any SAP-published interface and are still unchecked.

Still without a component: time-window preconditions need the window definition (the converter needs it, not the component); event waits need to know where the event is raised (`EventAutomationComponent` or a sensor).
