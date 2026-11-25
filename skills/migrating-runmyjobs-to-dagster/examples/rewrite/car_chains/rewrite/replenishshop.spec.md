# Specification: RunMyJobs chain `ReplenishShop`

Facts read from the extracted model (what RunMyJobs holds) and from the converter's report (what the converter does with this unit today). Statements marked *inferred* are not facts. No secret value is in this file: credential-like parameter values and credential-looking assignments in commands are replaced, and connections are names only.

- application: (none); description: Replenish a Shop; unit id `replenishshop`
- time zones named: none; run window (chain or process): none

## What it does (from the facts)

- a chain of 4 step(s) with 4 call(s) of type(s): os
- its own description reads: 'Replenish a Shop'
- its final call(s) are ReplenishShop/confirmation truck loaded (calls nothing else waits for)
- every call is the RunMyJobs no-op built-in System_DoNothing: the chain is an orchestration skeleton (placeholders for later jobs) *inferred*
- no submit frame or file event in the export starts it (it may be started by a person, another chain or an event) *inferred*

## What the converter does with it today

- disposition: **translated** (report status `converted`); collapsed to one asset: True; assets: replenishshop
- stubbed (fails with RMJ-CAVEAT until ported): none
- degraded (translated with a stated difference): none
  - 4 steps run in order as one asset and stop at the first failure; use chains=assets for one asset per call
- manifest units: U032, U033, U034, U035, U036

## Parameters

- `P_REGION` (in, String): default none; mandatory
- `P_AREA` (in, String): default none; mandatory

## Graph

Steps run in order; calls in one step run in parallel, and every call waits for all calls of the step before.

1. step `Wait for Truck`: `ReplenishShop/wait for truck`
2. step `Calculate Replenishment`: `ReplenishShop/calculate replenishment`
3. step `Pick Orders`: `ReplenishShop/pick orders`
4. step `Confirmation Truck loaded`: `ReplenishShop/confirmation truck loaded`
- edges (earlier -> later): ReplenishShop/wait for truck -> ReplenishShop/calculate replenishment, ReplenishShop/calculate replenishment -> ReplenishShop/pick orders, ReplenishShop/pick orders -> ReplenishShop/confirmation truck loaded

## Calls

### `ReplenishShop/wait for truck` (os, definition `System_DoNothing`)

- step: Wait for Truck; upstream: none; application (none)
- queue: none; Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U032 -> step wait_for_truck of asset replenishshop [converted]
- command / SQL / script (verbatim):

```
echo System_DoNothing: RunMyJobs built-in, does nothing
```

### `ReplenishShop/calculate replenishment` (os, definition `System_DoNothing`)

- step: Calculate Replenishment; upstream: ReplenishShop/wait for truck; application (none)
- queue: none; Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U033 -> step calculate_replenishment of asset replenishshop [converted]
- command / SQL / script (verbatim):

```
echo System_DoNothing: RunMyJobs built-in, does nothing
```

### `ReplenishShop/pick orders` (os, definition `System_DoNothing`)

- step: Pick Orders; upstream: ReplenishShop/calculate replenishment; application (none)
- queue: none; Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U034 -> step pick_orders of asset replenishshop [converted]
- command / SQL / script (verbatim):

```
echo System_DoNothing: RunMyJobs built-in, does nothing
```

### `ReplenishShop/confirmation truck loaded` (os, definition `System_DoNothing`)

- step: Confirmation Truck loaded; upstream: ReplenishShop/pick orders; application (none)
- queue: none; Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U035 -> step confirmation_truck_loaded of asset replenishshop [converted]
- command / SQL / script (verbatim):

```
echo System_DoNothing: RunMyJobs built-in, does nothing
```

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

- `ReplenishShop/wait for truck` (call): System_DoNothing (os)
- `ReplenishShop/calculate replenishment` (call): System_DoNothing (os)
- `ReplenishShop/pick orders` (call): System_DoNothing (os)
- `ReplenishShop/confirmation truck loaded` (call): System_DoNothing (os)
