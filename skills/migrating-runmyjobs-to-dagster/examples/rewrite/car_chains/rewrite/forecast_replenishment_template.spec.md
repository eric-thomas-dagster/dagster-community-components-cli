# Specification: RunMyJobs chain `Forecast_Replenishment_Template`

Facts read from the extracted model (what RunMyJobs holds) and from the converter's report (what the converter does with this unit today). Statements marked *inferred* are not facts. No secret value is in this file: credential-like parameter values and credential-looking assignments in commands are replaced, and connections are names only.

- application: (none); description: Forecast and Replenishment Daily Overall Chain; unit id `forecast_replenishment_template`
- time zones named: none; run window (chain or process): none

## What it does (from the facts)

- a chain of 5 step(s) with 16 call(s) of type(s): os
- its own description reads: 'Forecast and Replenishment Daily Overall Chain'
- its final call(s) are Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 1/confirmation truck loaded, Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 2/confirmation truck loaded, Forecast_Replenishment_Template/process negative stock/create pids (calls nothing else waits for)
- every call is the RunMyJobs no-op built-in System_DoNothing: the chain is an orchestration skeleton (placeholders for later jobs) *inferred*
- no submit frame or file event in the export starts it (it may be started by a person, another chain or an event) *inferred*

## What the converter does with it today

- disposition: **degraded** (report status `needs review`); collapsed to one asset: False; assets: forecast_replenishment_template__initialize, forecast_replenishment_template__post_posdm_idocs, forecast_replenishment_template__reprocess_idocs_in_error, forecast_replenishment_template__post_pids, forecast_replenishment_template__full_forecast_and_replenishment_run__preparation_run, forecast_replenishment_template__full_forecast_and_replenishment_run__promotion_forecast, forecast_replenishment_template__full_forecast_and_replenishment_run__replenish_shop_1__wait_for_truck, forecast_replenishment_template__full_forecast_and_replenishment_run__replenish_shop_1__calculate_replenishment, forecast_replenishment_template__full_forecast_and_replenishment_run__replenish_shop_1__pick_orders, forecast_replenishment_template__full_forecast_and_replenishment_run__replenish_shop_1__confirmation_truck_loaded, forecast_replenishment_template__full_forecast_and_replenishment_run__replenish_shop_2__wait_for_truck, forecast_replenishment_template__full_forecast_and_replenishment_run__replenish_shop_2__calculate_replenishment, forecast_replenishment_template__full_forecast_and_replenishment_run__replenish_shop_2__pick_orders, forecast_replenishment_template__full_forecast_and_replenishment_run__replenish_shop_2__confirmation_truck_loaded, forecast_replenishment_template__process_negative_stock__count_negative_stock, forecast_replenishment_template__process_negative_stock__create_pids
- stubbed (fails with RMJ-CAVEAT until ported): none
- degraded (translated with a stated difference): Forecast_Replenishment_Template
  - kept as one asset per call: step 'Nightly Processing' runs 2 calls in parallel, which one chain asset cannot express
  - step 'Nightly Processing' calls chain 'Full_FR_Run': its steps are inlined here as assets named forecast_replenishment_template__full_forecast_and_replenishment_run__*; parameter 'P_REGION' (from 'P_REGION') has no value here (the caller supplies it when the chain is submitted)
  - step 'Nightly Processing' calls chain 'Process_Negative_Stock_Template': its steps are inlined here as assets named forecast_replenishment_template__process_negative_stock__*; parameter 'P_REGION' (from 'P_REGION') has no value here (the caller supplies it when the chain is submitted)
  - step 'Replenish the Shops' calls chain 'ReplenishShop': its steps are inlined here as assets named forecast_replenishment_template__full_forecast_and_replenishment_run__replenish_shop_1__*; parameter 'P_REGION' (from 'P_REGION') has no value here (the caller supplies it when the chain is submitted)
  - step 'Replenish the Shops' calls chain 'ReplenishShop': its steps are inlined here as assets named forecast_replenishment_template__full_forecast_and_replenishment_run__replenish_shop_2__*; parameter 'P_REGION' (from 'P_REGION') has no value here (the caller supplies it when the chain is submitted)
  - the call to 'ReplenishShop' waits for event(s) WaitForShop1: not enforced (see the first inlined step)
  - the call to 'ReplenishShop' waits for event(s) WaitForShop2: not enforced (see the first inlined step)
- manifest units: U001, U002, U003, U004, U005, U006, U007, U008, U009, U010, U011, U012, U013, U014, U015, U016, U017

## Parameters

- `P_REGION` (in, String): default none; mandatory

## Graph

Steps run in order; calls in one step run in parallel, and every call waits for all calls of the step before.

1. step `Initialize`: `Forecast_Replenishment_Template/initialize`
2. step `Post POSDM Idocs`: `Forecast_Replenishment_Template/post posdm idocs`
3. step `Reprocess Idocs in Error`: `Forecast_Replenishment_Template/reprocess idocs in error`
4. step `Post PIDs`: `Forecast_Replenishment_Template/post pids`
5. step `Nightly Processing`: (no calls)
- edges (earlier -> later): Forecast_Replenishment_Template/initialize -> Forecast_Replenishment_Template/post posdm idocs, Forecast_Replenishment_Template/post posdm idocs -> Forecast_Replenishment_Template/reprocess idocs in error, Forecast_Replenishment_Template/reprocess idocs in error -> Forecast_Replenishment_Template/post pids, Forecast_Replenishment_Template/post pids -> Forecast_Replenishment_Template/full forecast and replenishment run/preparation run, Forecast_Replenishment_Template/full forecast and replenishment run/preparation run -> Forecast_Replenishment_Template/full forecast and replenishment run/promotion forecast, Forecast_Replenishment_Template/full forecast and replenishment run/promotion forecast -> Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 1/wait for truck, Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 1/wait for truck -> Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 1/calculate replenishment, Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 1/calculate replenishment -> Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 1/pick orders, Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 1/pick orders -> Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 1/confirmation truck loaded, Forecast_Replenishment_Template/full forecast and replenishment run/promotion forecast -> Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 2/wait for truck, Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 2/wait for truck -> Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 2/calculate replenishment, Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 2/calculate replenishment -> Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 2/pick orders, Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 2/pick orders -> Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 2/confirmation truck loaded, Forecast_Replenishment_Template/post pids -> Forecast_Replenishment_Template/process negative stock/count negative stock, Forecast_Replenishment_Template/process negative stock/count negative stock -> Forecast_Replenishment_Template/process negative stock/create pids
- Forecast_Replenishment_Template/full forecast and replenishment run: calls chain Full_FR_Run; its steps are listed below with ids starting `Forecast_Replenishment_Template/full forecast and replenishment run/`
- Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 1: calls chain ReplenishShop; its steps are listed below with ids starting `Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 1/`
- Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 2: calls chain ReplenishShop; its steps are listed below with ids starting `Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 2/`
- Forecast_Replenishment_Template/process negative stock: calls chain Process_Negative_Stock_Template; its steps are listed below with ids starting `Forecast_Replenishment_Template/process negative stock/`

## Calls

### `Forecast_Replenishment_Template/initialize` (os, definition `System_DoNothing`)

- step: Initialize; upstream: none; application (none)
- queue: none; Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U001 -> forecast_replenishment_template__initialize [converted]
- command / SQL / script (verbatim):

```
echo System_DoNothing: RunMyJobs built-in, does nothing
```

### `Forecast_Replenishment_Template/post posdm idocs` (os, definition `System_DoNothing`)

- step: Post POSDM Idocs; upstream: Forecast_Replenishment_Template/initialize; application (none)
- queue: none; Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U002 -> forecast_replenishment_template__post_posdm_idocs [converted]
- command / SQL / script (verbatim):

```
echo System_DoNothing: RunMyJobs built-in, does nothing
```

### `Forecast_Replenishment_Template/reprocess idocs in error` (os, definition `System_DoNothing`)

- step: Reprocess Idocs in Error; upstream: Forecast_Replenishment_Template/post posdm idocs; application (none)
- queue: none; Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U003 -> forecast_replenishment_template__reprocess_idocs_in_error [converted]
- command / SQL / script (verbatim):

```
echo System_DoNothing: RunMyJobs built-in, does nothing
```

### `Forecast_Replenishment_Template/post pids` (os, definition `System_DoNothing`)

- step: Post PIDs; upstream: Forecast_Replenishment_Template/reprocess idocs in error; application (none)
- queue: none; Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U004 -> forecast_replenishment_template__post_pids [converted]
- command / SQL / script (verbatim):

```
echo System_DoNothing: RunMyJobs built-in, does nothing
```

### `Forecast_Replenishment_Template/full forecast and replenishment run/preparation run` (os, definition `System_DoNothing`)

- step: Preparation Run; upstream: Forecast_Replenishment_Template/post pids; application (none)
- queue: none; Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U005 -> forecast_replenishment_template__full_forecast_and_replenishment_run__preparation_run [converted]
- command / SQL / script (verbatim):

```
echo System_DoNothing: RunMyJobs built-in, does nothing
```

### `Forecast_Replenishment_Template/full forecast and replenishment run/promotion forecast` (os, definition `System_DoNothing`)

- step: Promotion Forecast; upstream: Forecast_Replenishment_Template/full forecast and replenishment run/preparation run; application (none)
- queue: none; Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U006 -> forecast_replenishment_template__full_forecast_and_replenishment_run__promotion_forecast [converted]
- command / SQL / script (verbatim):

```
echo System_DoNothing: RunMyJobs built-in, does nothing
```

### `Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 1/wait for truck` (os, definition `System_DoNothing`)

- step: Wait for Truck; upstream: Forecast_Replenishment_Template/full forecast and replenishment run/promotion forecast; application (none)
- queue: none; Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U007 -> forecast_replenishment_template__full_forecast_and_replenishment_run__replenish_shop_1__wait_for_truck [converted]
- converter: U011 -> forecast_replenishment_template__full_forecast_and_replenishment_run__replenish_shop_2__wait_for_truck [converted]
- command / SQL / script (verbatim):

```
echo System_DoNothing: RunMyJobs built-in, does nothing
```

### `Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 1/calculate replenishment` (os, definition `System_DoNothing`)

- step: Calculate Replenishment; upstream: Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 1/wait for truck; application (none)
- queue: none; Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U008 -> forecast_replenishment_template__full_forecast_and_replenishment_run__replenish_shop_1__calculate_replenishment [converted]
- converter: U012 -> forecast_replenishment_template__full_forecast_and_replenishment_run__replenish_shop_2__calculate_replenishment [converted]
- command / SQL / script (verbatim):

```
echo System_DoNothing: RunMyJobs built-in, does nothing
```

### `Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 1/pick orders` (os, definition `System_DoNothing`)

- step: Pick Orders; upstream: Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 1/calculate replenishment; application (none)
- queue: none; Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U009 -> forecast_replenishment_template__full_forecast_and_replenishment_run__replenish_shop_1__pick_orders [converted]
- converter: U013 -> forecast_replenishment_template__full_forecast_and_replenishment_run__replenish_shop_2__pick_orders [converted]
- command / SQL / script (verbatim):

```
echo System_DoNothing: RunMyJobs built-in, does nothing
```

### `Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 1/confirmation truck loaded` (os, definition `System_DoNothing`)

- step: Confirmation Truck loaded; upstream: Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 1/pick orders; application (none)
- queue: none; Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U010 -> forecast_replenishment_template__full_forecast_and_replenishment_run__replenish_shop_1__confirmation_truck_loaded [converted]
- converter: U014 -> forecast_replenishment_template__full_forecast_and_replenishment_run__replenish_shop_2__confirmation_truck_loaded [converted]
- command / SQL / script (verbatim):

```
echo System_DoNothing: RunMyJobs built-in, does nothing
```

### `Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 2/wait for truck` (os, definition `System_DoNothing`)

- step: Wait for Truck; upstream: Forecast_Replenishment_Template/full forecast and replenishment run/promotion forecast; application (none)
- queue: none; Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U007 -> forecast_replenishment_template__full_forecast_and_replenishment_run__replenish_shop_1__wait_for_truck [converted]
- converter: U011 -> forecast_replenishment_template__full_forecast_and_replenishment_run__replenish_shop_2__wait_for_truck [converted]
- command / SQL / script (verbatim):

```
echo System_DoNothing: RunMyJobs built-in, does nothing
```

### `Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 2/calculate replenishment` (os, definition `System_DoNothing`)

- step: Calculate Replenishment; upstream: Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 2/wait for truck; application (none)
- queue: none; Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U008 -> forecast_replenishment_template__full_forecast_and_replenishment_run__replenish_shop_1__calculate_replenishment [converted]
- converter: U012 -> forecast_replenishment_template__full_forecast_and_replenishment_run__replenish_shop_2__calculate_replenishment [converted]
- command / SQL / script (verbatim):

```
echo System_DoNothing: RunMyJobs built-in, does nothing
```

### `Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 2/pick orders` (os, definition `System_DoNothing`)

- step: Pick Orders; upstream: Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 2/calculate replenishment; application (none)
- queue: none; Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U009 -> forecast_replenishment_template__full_forecast_and_replenishment_run__replenish_shop_1__pick_orders [converted]
- converter: U013 -> forecast_replenishment_template__full_forecast_and_replenishment_run__replenish_shop_2__pick_orders [converted]
- command / SQL / script (verbatim):

```
echo System_DoNothing: RunMyJobs built-in, does nothing
```

### `Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 2/confirmation truck loaded` (os, definition `System_DoNothing`)

- step: Confirmation Truck loaded; upstream: Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 2/pick orders; application (none)
- queue: none; Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U010 -> forecast_replenishment_template__full_forecast_and_replenishment_run__replenish_shop_1__confirmation_truck_loaded [converted]
- converter: U014 -> forecast_replenishment_template__full_forecast_and_replenishment_run__replenish_shop_2__confirmation_truck_loaded [converted]
- command / SQL / script (verbatim):

```
echo System_DoNothing: RunMyJobs built-in, does nothing
```

### `Forecast_Replenishment_Template/process negative stock/count negative stock` (os, definition `System_DoNothing`)

- step: Count Negative Stock; upstream: Forecast_Replenishment_Template/post pids; application (none)
- queue: none; Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U015 -> forecast_replenishment_template__process_negative_stock__count_negative_stock [converted]
- command / SQL / script (verbatim):

```
echo System_DoNothing: RunMyJobs built-in, does nothing
```

### `Forecast_Replenishment_Template/process negative stock/create pids` (os, definition `System_DoNothing`)

- step: Create PIDs; upstream: Forecast_Replenishment_Template/process negative stock/count negative stock; application (none)
- queue: none; Maximum Starts: 1 (retries here = 0, the meaning of Maximum Starts is unconfirmed); timeout: None s; run as: default (not enforceable in Dagster)
- connection (name): none; success exit codes besides 0: none; run window: none; declared outputs: none
- resolved parameters: `{}`
- converter: U016 -> forecast_replenishment_template__process_negative_stock__create_pids [converted]
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

- `Forecast_Replenishment_Template/initialize` (call): System_DoNothing (os)
- `Forecast_Replenishment_Template/post posdm idocs` (call): System_DoNothing (os)
- `Forecast_Replenishment_Template/reprocess idocs in error` (call): System_DoNothing (os)
- `Forecast_Replenishment_Template/post pids` (call): System_DoNothing (os)
- `Forecast_Replenishment_Template/full forecast and replenishment run/preparation run` (call): System_DoNothing (os)
- `Forecast_Replenishment_Template/full forecast and replenishment run/promotion forecast` (call): System_DoNothing (os)
- `Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 1/wait for truck` (call): System_DoNothing (os)
- `Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 1/calculate replenishment` (call): System_DoNothing (os)
- `Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 1/pick orders` (call): System_DoNothing (os)
- `Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 1/confirmation truck loaded` (call): System_DoNothing (os)
- `Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 2/wait for truck` (call): System_DoNothing (os)
- `Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 2/calculate replenishment` (call): System_DoNothing (os)
- `Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 2/pick orders` (call): System_DoNothing (os)
- `Forecast_Replenishment_Template/full forecast and replenishment run/replenish shop 2/confirmation truck loaded` (call): System_DoNothing (os)
- `Forecast_Replenishment_Template/process negative stock/count negative stock` (call): System_DoNothing (os)
- `Forecast_Replenishment_Template/process negative stock/create pids` (call): System_DoNothing (os)
