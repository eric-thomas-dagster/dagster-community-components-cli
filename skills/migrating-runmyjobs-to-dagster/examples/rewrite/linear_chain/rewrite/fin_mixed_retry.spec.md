# Specification: RunMyJobs chain `FIN_MIXED_RETRY`

Facts read from the extracted model (what RunMyJobs holds) and from the converter's report (what the converter does with this unit today). Statements marked *inferred* are not facts. No secret value is in this file: credential-like parameter values and credential-looking assignments in commands are replaced, and connections are names only.

- application: (none); description: none; unit id `fin_mixed_retry`
- time zones named: none; run window (chain or process): none

## What it does (from the facts)

- a chain of 2 step(s) with 2 call(s) of type(s): os, sql
- its final call(s) are FIN_MIXED_RETRY/alone (calls nothing else waits for)
- it moves or prepares data with scripts, then writes it with SQL, a load-then-transform shape *inferred*
- it is started by file event(s): FIN_FILE_NO_CHECKS

## What the converter does with it today

- disposition: **degraded** (report status `converted`); collapsed to one asset: False; assets: fin_mixed_retry__pull, fin_mixed_retry__alone
- stubbed (fails with RMJ-CAVEAT until ported): none
- degraded (translated with a stated difference): FIN_FILE_NO_CHECKS
  - kept as one asset per call: steps have different retry counts (the component retries the whole chain)
  - no minimum size, age or stability check: a file still being written can start the job (RunMyJobs files events usually have one; check the source)
  - the sensor starts stopped; its cursor remembers files already seen, so turn it on once the parallel run starts
- manifest units: U005, U006, U007, U010

## Parameters

- none

## Graph

Steps run in order; calls in one step run in parallel, and every call waits for all calls of the step before.

1. step `s1`: `FIN_MIXED_RETRY/pull`
2. step `s2`: `FIN_MIXED_RETRY/alone`
- edges (earlier -> later): FIN_MIXED_RETRY/pull -> FIN_MIXED_RETRY/alone

## Calls

### `FIN_MIXED_RETRY/pull` (os, definition `PULL`)

- step: s1; upstream: none; application FIN
- queue: BATCH_QUEUE (the converter makes it pool `batch_queue`); Maximum Starts: 2 (retries here = 1, the meaning of Maximum Starts is unconfirmed); timeout: 600 s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: [3]; run window: none; declared outputs: none
- resolved parameters: `{"run_date": "today"}`
- converter: U005 -> fin_mixed_retry__pull [converted]; 'Maximum Starts' interpreted as total attempts; verify against how the source team uses it
- command / SQL / script (verbatim):

```
echo pull ${run_date}
```

### `FIN_MIXED_RETRY/alone` (sql, definition `PUBLISH_ALONE`)

- step: s2; upstream: FIN_MIXED_RETRY/pull; application FIN
- queue: BATCH_QUEUE (the converter makes it pool `batch_queue`); Maximum Starts: 3 (retries here = 2, the meaning of Maximum Starts is unconfirmed); timeout: 120 s; run as: default (not enforceable in Dagster)
- connection (name): WAREHOUSE; success exit codes besides 0: none; run window: 22:00-23:59 America/New_York mon,tue; declared outputs: none
- resolved parameters: `{}`
- converter: U006 -> fin_mixed_retry__alone [converted]; SQL step: set env var RMJ_CONN_WAREHOUSE to a SQLAlchemy URL for the target database (components output: define a resource named 'warehouse'); 'Maximum Starts' interpreted as total attempts; verify against how the source team uses it
- command / SQL / script (verbatim):

```
select 2
```

## Schedules and calendars

- none in the export (the extractor warns when a CAR export holds no submit frames; named calendars are not parsed)
- file event `FIN_FILE_NO_CHECKS`: directory `/data/inbound/other`, pattern `*`, poll 30 s, min size None, min age None, stable-size check False

## Queues

- queue `BATCH_QUEUE` -> pool `batch_queue`: limit 3 (caps steps running at once across all runs; the limit is set on the Dagster instance); declared in export: True; used by FIN_MIXED_RETRY/pull, FIN_MIXED_RETRY/alone; shared with units: FIN_NIGHTLY, FIN_NIGHTLY, FIN_NIGHTLY

## Preconditions, status handlers, events, notifications

- none

## Connections and endpoints (names only)

- `WAREHOUSE`. The converter reads a SQLAlchemy URL from `RMJ_CONN_<NAME>`; SAP and SMTP settings come from `SAP_*` / `SMTP_*` environment variables. Never write a value.

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

- `FIN_MIXED_RETRY/pull` (call): PULL (os)
- `FIN_MIXED_RETRY/alone` (call): PUBLISH_ALONE (sql)
- `BATCH_QUEUE` (queue): queue (pool batch_queue)
- `FIN_FILE_NO_CHECKS` (file_event): file event on /data/inbound/other
- `FIN_MIXED_RETRY/alone#run_window` (run_window): call runs only inside 22:00-23:59 America/New_York mon,tue; outside it the run is skipped (not failed)
