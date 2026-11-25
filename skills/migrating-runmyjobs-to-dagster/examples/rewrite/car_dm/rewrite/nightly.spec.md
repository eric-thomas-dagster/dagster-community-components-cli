# Specification: RunMyJobs chain `NIGHTLY`

Facts read from the extracted model (what RunMyJobs holds) and from the converter's report (what the converter does with this unit today). Statements marked *inferred* are not facts. No secret value is in this file: credential-like parameter values and credential-looking assignments in commands are replaced, and connections are names only.

- application: (none); description: none; unit id `nightly`
- time zones named: UTC; run window (chain or process): none

## What it does (from the facts)

- a chain of 2 step(s) with 2 call(s) of type(s): os
- its final call(s) are NIGHTLY/load_1 (calls nothing else waits for)
- it is started by 1 submit frame(s): JG

## What the converter does with it today

- disposition: **degraded** (report status `needs review`); collapsed to one asset: False; assets: nightly__extract_1, nightly__load_1
- stubbed (fails with RMJ-CAVEAT until ported): none
- degraded (translated with a stated difference): NIGHTLY/load_1 (LOAD), NIGHTLY
  - chain-level restart count is not converted
  - kept as one asset per call: steps have different retry counts (the component retries the whole chain)
  - step 'load' precondition {'time_window': 'BIZ'} not enforced
- manifest units: U001, U002, U003, U004

## Parameters

- none

## Graph

Steps run in order; calls in one step run in parallel, and every call waits for all calls of the step before.

1. step `extract`: `NIGHTLY/extract_1`
2. step `load`: `NIGHTLY/load_1`
- edges (earlier -> later): NIGHTLY/extract_1 -> NIGHTLY/load_1

## Calls

### `NIGHTLY/extract_1` (os, definition `EXTRACT`)

- step: extract; upstream: none; application (none)
- queue: none; Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- step status handler: on status Error: restart_step, max=2
- converter: U001 -> nightly__extract_1 [converted]
- command / SQL / script (verbatim):

```
echo hi
```

### `NIGHTLY/load_1` (os, definition `LOAD`)

- step: load; upstream: NIGHTLY/extract_1; application (none)
- queue: none; Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U002 -> nightly__load_1 [needs review/degraded]; step 'load' precondition {'time_window': 'BIZ'} not enforced
- command / SQL / script (verbatim):

```
echo load
```

## Schedules and calendars

- `JG`: time zone `UTC`; elements: `[{"at": "02:30", "kind": "daily"}]`
  - element {"at": "02:30", "kind": "daily"} -> cron `30 2 * * *`

## Queues

- none

## Preconditions, status handlers, events, notifications

- precondition at step load: time window BIZ must be open (the time window's own definition is not in the export)
- status handler at step extract: on status Error: restart_step, max=2
- extractor note on the chain: chain-level restart count is not converted

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

- `NIGHTLY/extract_1` (call): EXTRACT (os)
- `NIGHTLY/load_1` (call): LOAD (os)
- `JG` (schedule): submit frame, time zone UTC
- `NIGHTLY#precondition1` (precondition): step load: time window BIZ must be open
- `NIGHTLY#handler1` (status_handler): step extract: on status Error: restart_step, max=2
- `NIGHTLY#note1` (extractor_note): the extractor saw but did not convert: chain-level restart count is not converted
