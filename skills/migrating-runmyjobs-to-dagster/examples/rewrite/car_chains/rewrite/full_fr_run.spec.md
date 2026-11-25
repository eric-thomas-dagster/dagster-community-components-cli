# Specification: RunMyJobs chain `Full_FR_Run`

Facts read from the extracted model (what RunMyJobs holds) and from the converter's report (what the converter does with this unit today). Statements marked *inferred* are not facts. No secret value is in this file: credential-like parameter values and credential-looking assignments in commands are replaced, and connections are names only.

- application: (none); description: Full Forecast and Replenishment run; unit id `full_fr_run`
- time zones named: none; run window (chain or process): none

## What it does (from the facts)

- a chain of 3 step(s) with 10 call(s) of type(s): os
- its own description reads: 'Full Forecast and Replenishment run'
- its final call(s) are Full_FR_Run/replenish shop 1/confirmation truck loaded, Full_FR_Run/replenish shop 2/confirmation truck loaded (calls nothing else waits for)
- every call is the RunMyJobs no-op built-in System_DoNothing: the chain is an orchestration skeleton (placeholders for later jobs) *inferred*
- no submit frame or file event in the export starts it (it may be started by a person, another chain or an event) *inferred*

## What the converter does with it today

- disposition: **degraded** (report status `needs review`); collapsed to one asset: False; assets: full_fr_run__preparation_run, full_fr_run__promotion_forecast, full_fr_run__replenish_shop_1__wait_for_truck, full_fr_run__replenish_shop_1__calculate_replenishment, full_fr_run__replenish_shop_1__pick_orders, full_fr_run__replenish_shop_1__confirmation_truck_loaded, full_fr_run__replenish_shop_2__wait_for_truck, full_fr_run__replenish_shop_2__calculate_replenishment, full_fr_run__replenish_shop_2__pick_orders, full_fr_run__replenish_shop_2__confirmation_truck_loaded
- stubbed (fails with RMJ-CAVEAT until ported): none
- degraded (translated with a stated difference): Full_FR_Run
  - kept as one asset per call: step 'Replenish the Shops' runs 2 calls in parallel, which one chain asset cannot express
  - step 'Replenish the Shops' calls chain 'ReplenishShop': its steps are inlined here as assets named full_fr_run__replenish_shop_1__*; parameter 'P_REGION' (from 'P_REGION') has no value here (the caller supplies it when the chain is submitted)
  - step 'Replenish the Shops' calls chain 'ReplenishShop': its steps are inlined here as assets named full_fr_run__replenish_shop_2__*; parameter 'P_REGION' (from 'P_REGION') has no value here (the caller supplies it when the chain is submitted)
  - the call to 'ReplenishShop' waits for event(s) WaitForShop1: not enforced (see the first inlined step)
  - the call to 'ReplenishShop' waits for event(s) WaitForShop2: not enforced (see the first inlined step)
- manifest units: U018, U019, U020, U021, U022, U023, U024, U025, U026, U027, U028

## Parameters

- `P_REGION` (in, String): default none; mandatory

## Graph

Steps run in order; calls in one step run in parallel, and every call waits for all calls of the step before.

1. step `Preparation Run`: `Full_FR_Run/preparation run`
2. step `Promotion Forecast`: `Full_FR_Run/promotion forecast`
3. step `Replenish the Shops`: (no calls)
- edges (earlier -> later): Full_FR_Run/preparation run -> Full_FR_Run/promotion forecast, Full_FR_Run/promotion forecast -> Full_FR_Run/replenish shop 1/wait for truck, Full_FR_Run/replenish shop 1/wait for truck -> Full_FR_Run/replenish shop 1/calculate replenishment, Full_FR_Run/replenish shop 1/calculate replenishment -> Full_FR_Run/replenish shop 1/pick orders, Full_FR_Run/replenish shop 1/pick orders -> Full_FR_Run/replenish shop 1/confirmation truck loaded, Full_FR_Run/promotion forecast -> Full_FR_Run/replenish shop 2/wait for truck, Full_FR_Run/replenish shop 2/wait for truck -> Full_FR_Run/replenish shop 2/calculate replenishment, Full_FR_Run/replenish shop 2/calculate replenishment -> Full_FR_Run/replenish shop 2/pick orders, Full_FR_Run/replenish shop 2/pick orders -> Full_FR_Run/replenish shop 2/confirmation truck loaded
- Full_FR_Run/replenish shop 1: calls chain ReplenishShop; its steps are listed below with ids starting `Full_FR_Run/replenish shop 1/`
- Full_FR_Run/replenish shop 2: calls chain ReplenishShop; its steps are listed below with ids starting `Full_FR_Run/replenish shop 2/`

## Calls

### `Full_FR_Run/preparation run` (os, definition `System_DoNothing`)

- step: Preparation Run; upstream: none; application (none)
- queue: none; Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U018 -> full_fr_run__preparation_run [converted]
- command / SQL / script (verbatim):

```
echo System_DoNothing: RunMyJobs built-in, does nothing
```

### `Full_FR_Run/promotion forecast` (os, definition `System_DoNothing`)

- step: Promotion Forecast; upstream: Full_FR_Run/preparation run; application (none)
- queue: none; Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U019 -> full_fr_run__promotion_forecast [converted]
- command / SQL / script (verbatim):

```
echo System_DoNothing: RunMyJobs built-in, does nothing
```

### `Full_FR_Run/replenish shop 1/wait for truck` (os, definition `System_DoNothing`)

- step: Wait for Truck; upstream: Full_FR_Run/promotion forecast; application (none)
- queue: none; Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U020 -> full_fr_run__replenish_shop_1__wait_for_truck [converted]
- converter: U024 -> full_fr_run__replenish_shop_2__wait_for_truck [converted]
- command / SQL / script (verbatim):

```
echo System_DoNothing: RunMyJobs built-in, does nothing
```

### `Full_FR_Run/replenish shop 1/calculate replenishment` (os, definition `System_DoNothing`)

- step: Calculate Replenishment; upstream: Full_FR_Run/replenish shop 1/wait for truck; application (none)
- queue: none; Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U021 -> full_fr_run__replenish_shop_1__calculate_replenishment [converted]
- converter: U025 -> full_fr_run__replenish_shop_2__calculate_replenishment [converted]
- command / SQL / script (verbatim):

```
echo System_DoNothing: RunMyJobs built-in, does nothing
```

### `Full_FR_Run/replenish shop 1/pick orders` (os, definition `System_DoNothing`)

- step: Pick Orders; upstream: Full_FR_Run/replenish shop 1/calculate replenishment; application (none)
- queue: none; Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U022 -> full_fr_run__replenish_shop_1__pick_orders [converted]
- converter: U026 -> full_fr_run__replenish_shop_2__pick_orders [converted]
- command / SQL / script (verbatim):

```
echo System_DoNothing: RunMyJobs built-in, does nothing
```

### `Full_FR_Run/replenish shop 1/confirmation truck loaded` (os, definition `System_DoNothing`)

- step: Confirmation Truck loaded; upstream: Full_FR_Run/replenish shop 1/pick orders; application (none)
- queue: none; Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U023 -> full_fr_run__replenish_shop_1__confirmation_truck_loaded [converted]
- converter: U027 -> full_fr_run__replenish_shop_2__confirmation_truck_loaded [converted]
- command / SQL / script (verbatim):

```
echo System_DoNothing: RunMyJobs built-in, does nothing
```

### `Full_FR_Run/replenish shop 2/wait for truck` (os, definition `System_DoNothing`)

- step: Wait for Truck; upstream: Full_FR_Run/promotion forecast; application (none)
- queue: none; Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U020 -> full_fr_run__replenish_shop_1__wait_for_truck [converted]
- converter: U024 -> full_fr_run__replenish_shop_2__wait_for_truck [converted]
- command / SQL / script (verbatim):

```
echo System_DoNothing: RunMyJobs built-in, does nothing
```

### `Full_FR_Run/replenish shop 2/calculate replenishment` (os, definition `System_DoNothing`)

- step: Calculate Replenishment; upstream: Full_FR_Run/replenish shop 2/wait for truck; application (none)
- queue: none; Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U021 -> full_fr_run__replenish_shop_1__calculate_replenishment [converted]
- converter: U025 -> full_fr_run__replenish_shop_2__calculate_replenishment [converted]
- command / SQL / script (verbatim):

```
echo System_DoNothing: RunMyJobs built-in, does nothing
```

### `Full_FR_Run/replenish shop 2/pick orders` (os, definition `System_DoNothing`)

- step: Pick Orders; upstream: Full_FR_Run/replenish shop 2/calculate replenishment; application (none)
- queue: none; Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U022 -> full_fr_run__replenish_shop_1__pick_orders [converted]
- converter: U026 -> full_fr_run__replenish_shop_2__pick_orders [converted]
- command / SQL / script (verbatim):

```
echo System_DoNothing: RunMyJobs built-in, does nothing
```

### `Full_FR_Run/replenish shop 2/confirmation truck loaded` (os, definition `System_DoNothing`)

- step: Confirmation Truck loaded; upstream: Full_FR_Run/replenish shop 2/pick orders; application (none)
- queue: none; Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U023 -> full_fr_run__replenish_shop_1__confirmation_truck_loaded [converted]
- converter: U027 -> full_fr_run__replenish_shop_2__confirmation_truck_loaded [converted]
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

- `Full_FR_Run/preparation run` (call): System_DoNothing (os)
- `Full_FR_Run/promotion forecast` (call): System_DoNothing (os)
- `Full_FR_Run/replenish shop 1/wait for truck` (call): System_DoNothing (os)
- `Full_FR_Run/replenish shop 1/calculate replenishment` (call): System_DoNothing (os)
- `Full_FR_Run/replenish shop 1/pick orders` (call): System_DoNothing (os)
- `Full_FR_Run/replenish shop 1/confirmation truck loaded` (call): System_DoNothing (os)
- `Full_FR_Run/replenish shop 2/wait for truck` (call): System_DoNothing (os)
- `Full_FR_Run/replenish shop 2/calculate replenishment` (call): System_DoNothing (os)
- `Full_FR_Run/replenish shop 2/pick orders` (call): System_DoNothing (os)
- `Full_FR_Run/replenish shop 2/confirmation truck loaded` (call): System_DoNothing (os)
