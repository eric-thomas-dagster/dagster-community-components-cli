# Specification: RunMyJobs process definition `PURGE_TEMP`

Facts read from the extracted model (what RunMyJobs holds) and from the converter's report (what the converter does with this unit today). Statements marked *inferred* are not facts. No secret value is in this file: credential-like parameter values and credential-looking assignments in commands are replaced, and connections are names only.

- application: OPS; description: none; unit id `purge_temp`
- time zones named: America/Chicago; run window (chain or process): none

## What it does (from the facts)

- a standalone os process definition in application OPS
- it is started by 1 submit frame(s): PURGE_HOURLY

## What the converter does with it today

- disposition: **degraded** (report status `converted`); collapsed to one asset: False; assets: purge_temp
- stubbed (fails with RMJ-CAVEAT until ported): none
- degraded (translated with a stated difference): PURGE_HOURLY
  - 'last day of month' has no cron equivalent; needs a custom schedule function
  - multiple submit-frame elements become one Dagster schedule each (union of cycles)
  - time window 08:00-18:00 mapped to hour range 8-17 (hour granularity)
- manifest units: U008, U011

## Parameters

- none

## Graph

- edges (earlier -> later): none

## Calls

### `PURGE_TEMP` (os, definition `PURGE_TEMP`)

- step: (standalone); upstream: none; application OPS
- queue: none; Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: svc_batch (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U008 -> purge_temp [converted]
- command / SQL / script (verbatim):

```
echo purging
```

## Schedules and calendars

- `PURGE_HOURLY`: time zone `America/Chicago`; elements: `[{"every": 15, "kind": "interval", "unit": "minutes", "window": {"days": ["mon", "tue", "wed", "thu", "fri"], "end": "18:00", "start": "08:00"}}, {"at": "23:00", "day": "last", "kind": "monthly"}]`
  - element {"every": 15, "kind": "interval", "unit": "minutes", "window": {"days": ["mon", "tue", "wed", "thu", "fri"], "end": "18:00", "start": "08:00"}} -> cron `*/15 8-17 * * 1,2,3,4,5`
    - time window 08:00-18:00 mapped to hour range 8-17 (hour granularity)
  - element {"at": "23:00", "day": "last", "kind": "monthly"} has NO cron form
    - 'last day of month' has no cron equivalent; needs a custom schedule function

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

- `PURGE_TEMP` (call): PURGE_TEMP (os)
- `PURGE_HOURLY` (schedule): submit frame, time zone America/Chicago
