# Rewrite RunMyJobs chain `FIN_NIGHTLY` as idiomatic Dagster

You are writing the Dagster code for one RunMyJobs unit from scratch. Do not transform the RunMyJobs objects one for one: understand what the unit does from the specification below, then build what
a Dagster engineer would build for that behavior. The converter's output is a reference draft and the fallback if your rewrite fails the gates; it is not the model to imitate.

## 1. Load the skills and follow them

1. Load the **`dagster-expert`** skill (by name) and follow its component hierarchy: a community registry component first, an official `dagster-<vendor>` integration next,
   hand-written code last. Create the project with `uvx create-dagster project <name> --uv-sync` if it does not exist (never by hand) and scaffold with `dg scaffold defs`. Check every Dagster
   name and signature with it, with `dg` (`dg list components --json`, `--help`) or with the installed source. Do not invent an API.
2. Load the **`dagster-community-components`** skill (by name; for agents that load skills from files: `skills/dagster-community-components/SKILL.md` in this repository, with `reference/cli.md`, `reference/catalog.md`,
   `reference/writing-yaml.md` and `reference/demo-workflow.md`). Its hard rules apply: never invent a component id, config field or CLI flag; search by capability and vendor before concluding
   nothing exists; read the real schema before writing any `defs.yaml`; registry component first, official integration second, hand-written code last.
3. Follow `reference/demo-workflow.md` of that skill for every stage of this unit: **search** (`dagster-component search "<stage keywords>" --json`), **verify** that stages chain (`produces` /
   `consumes`, the README's `Pairs with`), **read the schema** (`dagster-component info <id>`, `dagster-component schema <id> --format json`), **add and wire** (`dagster-component add <id>` or
   `type: dagster_community_components.<Class>` with `--as-package`, in a `defs.yaml` that uses only fields you read), **validate** (`dg check defs`, `dg list defs --json`).
   `dagster-component list --available` lists the registry (plain `list` shows what is installed).
4. For each registry component you use, say in the header docstring which id you used and which schema fields you set, and name its validation tier (`validation_level` in
   `dagster-component search --json`). A component that is not `live` carries a caveat sentence. If you use an official integration or hand-written code instead, say why no registry component fit.

## 2. The specification (facts; statements marked inferred are not facts)

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


## 3. What to build: the target idioms

- **One asset per data product or job outcome.** A data product is a step whose output is persisted (a table, a file, a loaded fact) or consumed by more than the next step. Steps that only prepare
  or help produce one output may be part of the asset that needs them (one command or function), and the header says so.
- **A chain becomes asset dependencies, or a task-chain component.** A linear chain of command, SQL and script steps whose queue, retries and window agree may be ONE `TaskChainAssetComponent`
  (read its schema first); parallel calls in a step become assets that share the same upstream dependencies. Keep the order in the spec: every call waits for all calls of the step before it.
- **Queues become pools** (`pool="<queue name in lower_snake>"` on the asset, or the component's `pool` field). A pool has no limit until one is set on the instance: list the
  `dagster instance concurrency set <pool> <n>` commands in the header, using the limits in the spec. Several units may share one queue: use the same pool name.
- **Schedules become schedules or automation with an explicit time zone** (`dg.ScheduleDefinition(..., execution_timezone=...)`, or `CronScheduleComponent`). Use the cron and zone in the spec; a submit
  frame element with no cron form (for example "last day of month") needs a schedule function or an in-asset check, stated in the header. Keep schedules stopped by default (`default_status`).
- **Status handlers, notifications, preconditions and events are not steps.** A restart handler becomes a retry policy; "mark completed on error" becomes an explicit, logged continue; a failure
  alert or an email becomes a run-failure or run-status sensor, a hook or an alert (the notification targets in the spec are kept by name); a precondition becomes an asset check, a run window or an
  automation condition; a wait for an event becomes a sensor or an upstream asset. Say in the header which one, for each.
- **Connections and endpoints by name only.** SQL connections read a SQLAlchemy URL from the environment variable `RMJ_CONN_<CONNECTION NAME IN UPPER CASE>` (the converter's convention; the gate points it at a local SQLite database to materialize the unit); SAP and SMTP settings come from the environment. Never copy a
  secret, a password, a token or a URL with credentials into code, YAML, tests or the header.
- **Keep commands and SQL exactly as given** (they are verbatim in the spec) unless you reimplement the step, in which case the header line for that call says `reimplemented` and why.
- **Structural checks only where the facts show something checkable** (the output is non-empty, the command exited with an allowed code). Do not invent business rules or thresholds.
- **A loud stub is the only acceptable placeholder.** A step you cannot port (RedwoodScript, an SAP connector with no component, a missing definition) fails with `RMJ-CAVEAT: <reason>` in the
  right place in the graph, with its source kept in a comment. A body that returns success is never acceptable, and neither is silently leaving the step out.
- Preserve every behavior under "must be preserved" in the spec; the "acceptable differences" are yours to use.

## 4. Where to write it

- The unit's definitions go under `src/rmj_rewrite/defs/fin_nightly/` or in `src/rmj_rewrite/defs/fin_nightly.py`. A folder that holds a `defs.yaml` does not load Python modules next to it: put registry-component YAML in `fin_nightly/components/defs.yaml` and hand-written modules in `fin_nightly/python/` (with an `__init__.py` in each), and the header docstring in `fin_nightly/__init__.py`. Define a job named
  `fin_nightly` over the unit's assets (components output defines jobs through the schedule component; a hand-written module defines it with `dg.define_asset_job`) and the schedule(s) from the spec.
- The first lines of **every module you write** (and of `src/<pkg>/defs/<unit_slug>/__init__.py` when the unit is components-only YAML) are a docstring that accounts for every object under
  "Objects the header docstring must account for" in the spec: one line per object, `<id>: replaced by `<asset key>`` / `folded into `<asset key>`` / `merged into `<asset key>`` /
  `dropped: <reason>`; name only the replacing asset(s) or sensor(s) in backticks right after the verb and put details after a parenthesis or a semicolon (a later backticked name is read as another replacement); add `reimplemented` on a call whose command you did not keep verbatim. After that list: the registry components and tiers used, the pool commands, and every behavior you
  could not preserve or verify (stated, not hidden). A RunMyJobs object that is not accounted for fails the gate.
- Secrets: environment variables and resources only.

## 5. The acceptance gates

Your rewrite is accepted only when `runmyjobs-to-dagster accept rewrite/fin_nightly.spec.json --project <project root> --pkg <package> [--manifest migration_manifest.json]`
passes every gate (a gate that cannot run fails; it is never skipped silently):

1. **definitions load**: `dg check defs` passes in the project.
2. **definitions match the spec**: `dg list defs --json` and the loaded definitions show, for this unit: every RunMyJobs call accounted for in the header maps to an existing asset; each earlier-to-later
   pair in the spec's graph (2 here) is kept (the later asset is downstream of the earlier one, or both are folded into one asset); schedules equal the spec's
   (cron and time zone: `30 22 * * *` America/New_York); pools equal `batch_queue`; retries match
   the spec's per-call retries on the asset that holds the call.
3. **commands preserved**: each command or SQL text in the spec (3 here) appears in your code or YAML after normalising whitespace and parameter references, or its header line
   says `reimplemented`.
4. **completeness**: every object in the spec's accounting list (7 here) is named in a header docstring with what happened to it.
5. **generated pytest**: `tests/test_fin_nightly_rewrite.py` is written for you and run: the definitions load, the asset keys, dependencies, schedule and pools from the header are present, and, when the
   unit has no external system, the unit materializes with fake or local resources (a command that only echoes runs for real; SQL runs against a local SQLite URL). A unit with an external system
   (SAP, SMTP, a missing definition, a stub) is checked for shape only and the gate says why; there is no fake pass.
6. **structural asset checks** exist where the spec's facts support them (an output the converter derived a check for), and pass when materialized.
7. **no secrets**: a scan of your code, YAML and tests finds no credential-looking literal.
8. **no invented APIs**: every import resolves and every component `type:` is an importable class.

A rewrite that fails a gate stays **deferred** with the failure recorded; fix the cause and run the gates again. The manifest records `route: rewrite` and the gate result for every unit this spec
covers (U001, U002, U003, U004, U008, U009). Never mark a unit complete yourself and never edit the gate results.

## 6. The converter's output (reference and fallback)

The converter's output for this unit is under `converter_output` (disposition `degraded`; stubbed: none; degraded: FIN_NIGHTLY). Read it as a reference draft: it shows the source translated one call at a time. Do not copy its structure; use it to check that you have not lost behavior.

If the rewrite cannot pass the gates, the converter's output is what ships (a stubbed call stays pending or deferred as the converter left it). Say plainly in your final message what you could not
preserve or verify.

## 7. Cost

This route spends model effort on every unit (reading the spec, searching the registry, writing and iterating until the gates pass). Use it where idiomatic output matters or the converter stubs or
degrades the unit; the converter stays the default for what it translates cleanly.
