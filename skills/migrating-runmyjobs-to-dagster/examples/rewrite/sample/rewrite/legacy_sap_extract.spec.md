# Specification: RunMyJobs process definition `LEGACY_SAP_EXTRACT`

Facts read from the extracted model (what RunMyJobs holds) and from the converter's report (what the converter does with this unit today). Statements marked *inferred* are not facts. No secret value is in this file: credential-like parameter values and credential-looking assignments in commands are replaced, and connections are names only.

- application: OPS; description: none; unit id `legacy_sap_extract`
- time zones named: none; run window (chain or process): none

## What it does (from the facts)

- a standalone sap process definition in application OPS
- no submit frame or file event in the export starts it (it may be started by a person, another chain or an event) *inferred*

## What the converter does with it today

- disposition: **stubbed** (report status `unmapped`); collapsed to one asset: False; assets: legacy_sap_extract
- stubbed (fails with RMJ-CAVEAT until ported): LEGACY_SAP_EXTRACT
- degraded (translated with a stated difference): none
  - SAP connector job (a RunMyJobs SAP built-in): it needs an SAP integration, in Dagster or through the RunMyJobs connector
- manifest units: U009

## Parameters

- none

## Graph

- edges (earlier -> later): none

## Calls

### `LEGACY_SAP_EXTRACT` (sap, definition `LEGACY_SAP_EXTRACT`)

- step: (standalone); upstream: none; application OPS
- queue: none; Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U009 -> legacy_sap_extract [unmapped/stub]; SAP connector job (a RunMyJobs SAP built-in): it needs an SAP integration, in Dagster or through the RunMyJobs connector

## Schedules and calendars

- none in the export (the extractor warns when a CAR export holds no submit frames; named calendars are not parsed)

## Queues

- none

## Preconditions, status handlers, events, notifications

- none

## Connections and endpoints (names only)

- none. The converter reads a SQLAlchemy URL from `RMJ_CONN_<NAME>`; SAP and SMTP settings come from `SAP_*` / `SMTP_*` environment variables. Never write a value.

## Behaviours that must be preserved

- the order of steps and which calls may run in parallel (calls in one step run together; every call waits for all calls of the step before)
- every command, SQL statement and script exactly as given (the spec lists them verbatim), and the parameters passed to them
- the schedule: cron meaning, time zone, and any end date (a submit frame with several elements is a union of cycles)
- the concurrency each queue enforces (queue limits are shared across units, see ESTATE.md)
- retries from restart handlers and from Maximum Starts, timeouts, and which exit codes count as success
- what stops the run (a failing step stops the chain) and what continues (a status handler that marks a step completed on error)
- notification targets (who is emailed) and connection names; secrets and credentials come from the environment, never from code

## Differences that are acceptable

- asset boundaries and names: one asset per data product or job outcome is fine; steps that only prepare an output may fold into the asset that uses it
- a chain may become asset dependencies, a task-chain component, or one asset; it must keep the order above
- queues become Dagster pools (the limit is configured on the instance, not in code); schedules become Dagster schedules with an explicit time zone
- status handlers, preconditions and notifications may become hooks, sensors, asset checks, automation conditions or alerts instead of steps
- RunMyJobs-only constructs with no Dagster counterpart (run_as user, process server, priority) may be dropped if the header docstring says so
- 'last day of month' and minute-level time windows have no exact cron form: a schedule function or an in-asset window check is acceptable, a silent change is not

## Objects the header docstring must account for

One line per object in the form `<id>: replaced by `<asset key>`` / `folded into `<asset key>`` / `merged into `<asset key>`` / `dropped: <reason>` (a call whose text is not kept verbatim says `reimplemented`).

- `LEGACY_SAP_EXTRACT` (call): LEGACY_SAP_EXTRACT (sap)
