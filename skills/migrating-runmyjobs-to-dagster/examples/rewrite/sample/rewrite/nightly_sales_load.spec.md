# Specification: RunMyJobs chain `NIGHTLY_SALES_LOAD`

Facts read from the extracted model (what RunMyJobs holds) and from the converter's report (what the converter does with this unit today). Statements marked *inferred* are not facts. No secret value is in this file: credential-like parameter values and credential-looking assignments in commands are replaced, and connections are names only.

- application: SALES; description: none; unit id `nightly_sales_load`
- time zones named: America/Chicago; run window (chain or process): none

## What it does (from the facts)

- a chain of 3 step(s) with 6 call(s) of type(s): os, redwoodscript, sql
- its final call(s) are NIGHTLY_SALES_LOAD/margin, NIGHTLY_SALES_LOAD/notify (calls nothing else waits for)
- declared outputs of its processes: dw.fact_inventory, dw.fact_sales, landing.inventory_raw, landing.sales_raw
- it moves or prepares data with scripts, then writes it with SQL, a load-then-transform shape *inferred*
- it is started by 1 submit frame(s): NIGHTLY_SALES_SCHEDULE

## What the converter does with it today

- disposition: **stubbed** (report status `needs review`); collapsed to one asset: False; assets: nightly_sales_load__sales, nightly_sales_load__inventory, nightly_sales_load__sales_fact, nightly_sales_load__inventory_fact, nightly_sales_load__margin, nightly_sales_load__notify
- stubbed (fails with RMJ-CAVEAT until ported): NIGHTLY_SALES_LOAD/margin (CALC_MARGIN)
- degraded (translated with a stated difference): NIGHTLY_SALES_LOAD/notify (NOTIFY_DONE), NIGHTLY_SALES_LOAD
  - RedwoodScript is not auto-translated; port the source (kept in the asset body) by hand or with the planned LLM pass
  - kept as one asset per call: step 'extract' runs 2 calls in parallel, which one chain asset cannot express
  - step 'post' precondition {'time_window': 'BUSINESS_HOURS'} not enforced
- manifest units: U001, U002, U003, U004, U005, U006, U007, U010

## Parameters

- none

## Graph

Steps run in order; calls in one step run in parallel, and every call waits for all calls of the step before.

1. step `extract`: `NIGHTLY_SALES_LOAD/sales`, `NIGHTLY_SALES_LOAD/inventory`
2. step `load`: `NIGHTLY_SALES_LOAD/sales_fact`, `NIGHTLY_SALES_LOAD/inventory_fact`
3. step `post`: `NIGHTLY_SALES_LOAD/margin`, `NIGHTLY_SALES_LOAD/notify`
- edges (earlier -> later): NIGHTLY_SALES_LOAD/sales -> NIGHTLY_SALES_LOAD/sales_fact, NIGHTLY_SALES_LOAD/inventory -> NIGHTLY_SALES_LOAD/sales_fact, NIGHTLY_SALES_LOAD/sales -> NIGHTLY_SALES_LOAD/inventory_fact, NIGHTLY_SALES_LOAD/inventory -> NIGHTLY_SALES_LOAD/inventory_fact, NIGHTLY_SALES_LOAD/sales_fact -> NIGHTLY_SALES_LOAD/margin, NIGHTLY_SALES_LOAD/inventory_fact -> NIGHTLY_SALES_LOAD/margin, NIGHTLY_SALES_LOAD/sales_fact -> NIGHTLY_SALES_LOAD/notify, NIGHTLY_SALES_LOAD/inventory_fact -> NIGHTLY_SALES_LOAD/notify

## Calls

### `NIGHTLY_SALES_LOAD/sales` (os, definition `EXTRACT_SALES`)

- step: extract; upstream: none; application SALES
- queue: BATCH_QUEUE (the converter makes it pool `batch_queue`); Maximum Starts: 3 (retries here = 2, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: landing.sales_raw
- resolved parameters: `{"run_date": "today"}`
- step status handler: on status Error: restart_step, max=2
- converter: U001 -> nightly_sales_load__sales [converted]
- command / SQL / script (verbatim):

```
echo extracting sales for ${run_date}
```

### `NIGHTLY_SALES_LOAD/inventory` (os, definition `EXTRACT_INVENTORY`)

- step: extract; upstream: none; application SALES
- queue: BATCH_QUEUE (the converter makes it pool `batch_queue`); Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: landing.inventory_raw
- resolved parameters: `{}`
- step status handler: on status Error: restart_step, max=2
- converter: U002 -> nightly_sales_load__inventory [converted]
- command / SQL / script (verbatim):

```
echo extracting inventory
```

### `NIGHTLY_SALES_LOAD/sales_fact` (sql, definition `LOAD_SALES_FACT`)

- step: load; upstream: NIGHTLY_SALES_LOAD/sales, NIGHTLY_SALES_LOAD/inventory; application SALES
- queue: DB_QUEUE (the converter makes it pool `db_queue`); Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): WAREHOUSE; success exit codes besides 0: none; run window: none; declared outputs: dw.fact_sales
- resolved parameters: `{}`
- converter: U003 -> nightly_sales_load__sales_fact [converted]; SQL step: set env var RMJ_CONN_WAREHOUSE to a SQLAlchemy URL for the target database (components output: define a resource named 'warehouse')
- command / SQL / script (verbatim):

```
select 1
```

### `NIGHTLY_SALES_LOAD/inventory_fact` (sql, definition `LOAD_INVENTORY_FACT`)

- step: load; upstream: NIGHTLY_SALES_LOAD/sales, NIGHTLY_SALES_LOAD/inventory; application SALES
- queue: DB_QUEUE (the converter makes it pool `db_queue`); Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): WAREHOUSE; success exit codes besides 0: none; run window: none; declared outputs: dw.fact_inventory
- resolved parameters: `{}`
- converter: U004 -> nightly_sales_load__inventory_fact [converted]; SQL step: set env var RMJ_CONN_WAREHOUSE to a SQLAlchemy URL for the target database (components output: define a resource named 'warehouse')
- command / SQL / script (verbatim):

```
select 1
```

### `NIGHTLY_SALES_LOAD/margin` (redwoodscript, definition `CALC_MARGIN`)

- step: post; upstream: NIGHTLY_SALES_LOAD/sales_fact, NIGHTLY_SALES_LOAD/inventory_fact; application SALES
- queue: none; Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U005 -> nightly_sales_load__margin [needs review/stub]; step 'post' precondition {'time_window': 'BUSINESS_HOURS'} not enforced; RedwoodScript is not auto-translated; port the source (kept in the asset body) by hand or with the planned LLM pass
- command / SQL / script (verbatim):

```
Job j = jcsSession.getJobByName("x");
jcsOut.println("margin done");

```

### `NIGHTLY_SALES_LOAD/notify` (os, definition `NOTIFY_DONE`)

- step: post; upstream: NIGHTLY_SALES_LOAD/sales_fact, NIGHTLY_SALES_LOAD/inventory_fact; application SALES
- queue: none; Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U006 -> nightly_sales_load__notify [needs review/degraded]; step 'post' precondition {'time_window': 'BUSINESS_HOURS'} not enforced
- command / SQL / script (verbatim):

```
echo notifying
```

## Schedules and calendars

- `NIGHTLY_SALES_SCHEDULE`: time zone `America/Chicago`; elements: `[{"at": "02:30", "kind": "daily"}]`
  - element {"at": "02:30", "kind": "daily"} -> cron `30 2 * * *`

## Queues

- queue `BATCH_QUEUE` -> pool `batch_queue`: limit 4 (caps steps running at once across all runs; the limit is set on the Dagster instance); declared in export: True; used by NIGHTLY_SALES_LOAD/sales, NIGHTLY_SALES_LOAD/inventory; shared with units: none
- queue `DB_QUEUE` -> pool `db_queue`: limit 2 (caps steps running at once across all runs; the limit is set on the Dagster instance); declared in export: True; used by NIGHTLY_SALES_LOAD/sales_fact, NIGHTLY_SALES_LOAD/inventory_fact; shared with units: none

## Preconditions, status handlers, events, notifications

- precondition at step post: time window BUSINESS_HOURS must be open (the time window's own definition is not in the export)
- status handler at step extract: on status Error: restart_step, max=2

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

- `NIGHTLY_SALES_LOAD/sales` (call): EXTRACT_SALES (os)
- `NIGHTLY_SALES_LOAD/inventory` (call): EXTRACT_INVENTORY (os)
- `NIGHTLY_SALES_LOAD/sales_fact` (call): LOAD_SALES_FACT (sql)
- `NIGHTLY_SALES_LOAD/inventory_fact` (call): LOAD_INVENTORY_FACT (sql)
- `NIGHTLY_SALES_LOAD/margin` (call): CALC_MARGIN (redwoodscript)
- `NIGHTLY_SALES_LOAD/notify` (call): NOTIFY_DONE (os)
- `NIGHTLY_SALES_SCHEDULE` (schedule): submit frame, time zone America/Chicago
- `BATCH_QUEUE` (queue): queue (pool batch_queue)
- `DB_QUEUE` (queue): queue (pool db_queue)
- `NIGHTLY_SALES_LOAD#precondition1` (precondition): step post: time window BUSINESS_HOURS must be open
- `NIGHTLY_SALES_LOAD#handler1` (status_handler): step extract: on status Error: restart_step, max=2
