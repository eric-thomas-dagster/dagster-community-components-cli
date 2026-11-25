# Specification: RunMyJobs chain `FIN_NIGHTLY`

Facts read from the extracted model (what RunMyJobs holds) and from the converter's report (what the converter does with this unit today). Statements marked *inferred* are not facts. No secret value is in this file: credential-like parameter values and credential-looking assignments in commands are replaced, and connections are names only.

- application: FIN; description: Nightly finance publish.; unit id `fin_nightly`
- time zones named: America/New_York; run window (chain or process): 22:00-23:59 America/New_York mon,tue

## What it does (from the facts)

- a chain of 3 step(s) with 3 call(s) of type(s): os, python, sql
- its own description reads: 'Nightly finance publish.'
- its final call(s) are FIN_NIGHTLY/publish (calls nothing else waits for)
- it moves or prepares data with scripts, then writes it with SQL, a load-then-transform shape *inferred*
- it is started by 1 submit frame(s): FIN_SCHED
- it is started by file event(s): FIN_FILE_ARRIVED

## What the converter does with it today

- disposition: **degraded** (report status `needs review`); collapsed to one asset: True; assets: fin_nightly
- stubbed (fails with RMJ-CAVEAT until ported): none
- degraded (translated with a stated difference): FIN_NIGHTLY
  - 3 steps run in order as one asset and stop at the first failure; use chains=assets for one asset per call
  - per-step timeouts become one chain timeout of 900s (the sum of the steps that have one)
  - the sensor starts stopped; its cursor remembers files already seen, so turn it on once the parallel run starts
- manifest units: U001, U002, U003, U004, U008, U009

## Parameters

- none

## Graph

Steps run in order; calls in one step run in parallel, and every call waits for all calls of the step before.

1. step `s1`: `FIN_NIGHTLY/pull`
2. step `s2`: `FIN_NIGHTLY/transform`
3. step `s3`: `FIN_NIGHTLY/publish`
- edges (earlier -> later): FIN_NIGHTLY/pull -> FIN_NIGHTLY/transform, FIN_NIGHTLY/transform -> FIN_NIGHTLY/publish

## Calls

### `FIN_NIGHTLY/pull` (os, definition `PULL`)

- step: s1; upstream: none; application FIN
- queue: BATCH_QUEUE (the converter makes it pool `batch_queue`); Maximum Starts: 2 (retries here = 1, the meaning of Maximum Starts is unconfirmed); timeout: 600 s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: [3]; run window: none; declared outputs: none
- resolved parameters: `{"run_date": "today"}`
- converter: U001 -> step pull of asset fin_nightly [converted]; 'Maximum Starts' interpreted as total attempts; verify against how the source team uses it
- command / SQL / script (verbatim):

```
echo pull ${run_date}
```

### `FIN_NIGHTLY/transform` (python, definition `TRANSFORM`)

- step: s2; upstream: FIN_NIGHTLY/pull; application FIN
- queue: BATCH_QUEUE (the converter makes it pool `batch_queue`); Maximum Starts: 2 (retries here = 1, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U002 -> step transform of asset fin_nightly [converted]; 'Maximum Starts' interpreted as total attempts; verify against how the source team uses it
- command / SQL / script (verbatim):

```
print('transform')
```

### `FIN_NIGHTLY/publish` (sql, definition `PUBLISH`)

- step: s3; upstream: FIN_NIGHTLY/transform; application FIN
- queue: BATCH_QUEUE (the converter makes it pool `batch_queue`); Maximum Starts: 2 (retries here = 1, the meaning of Maximum Starts is unconfirmed); timeout: 300 s; run as: default (not enforceable in Dagster)
- connection (name): WAREHOUSE; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U003 -> step publish of asset fin_nightly [converted]; SQL step: set env var RMJ_CONN_WAREHOUSE to a SQLAlchemy URL for the target database (components output: define a resource named 'warehouse'); 'Maximum Starts' interpreted as total attempts; verify against how the source team uses it; SQL step: set env var RMJ_CONN_WAREHOUSE to a SQLAlchemy URL for the target database
- command / SQL / script (verbatim):

```
select 1
```

## Schedules and calendars

- `FIN_SCHED`: time zone `America/New_York`; elements: `[{"at": "22:30", "kind": "daily"}]`
  - element {"at": "22:30", "kind": "daily"} -> cron `30 22 * * *`
- file event `FIN_FILE_ARRIVED`: directory `/data/inbound/fin`, pattern `fin_*.csv`, poll 60 s, min size 1, min age 120, stable-size check True

## Queues

- queue `BATCH_QUEUE` -> pool `batch_queue`: limit 3 (caps steps running at once across all runs; the limit is set on the Dagster instance); declared in export: True; used by FIN_NIGHTLY/pull, FIN_NIGHTLY/transform, FIN_NIGHTLY/publish; shared with units: FIN_MIXED_RETRY, FIN_MIXED_RETRY

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

- `FIN_NIGHTLY/pull` (call): PULL (os)
- `FIN_NIGHTLY/transform` (call): TRANSFORM (python)
- `FIN_NIGHTLY/publish` (call): PUBLISH (sql)
- `FIN_SCHED` (schedule): submit frame, time zone America/New_York
- `BATCH_QUEUE` (queue): queue (pool batch_queue)
- `FIN_FILE_ARRIVED` (file_event): file event on /data/inbound/fin
- `FIN_NIGHTLY#run_window` (run_window): chain runs only inside 22:00-23:59 America/New_York mon,tue; outside it the run is skipped (not failed)
