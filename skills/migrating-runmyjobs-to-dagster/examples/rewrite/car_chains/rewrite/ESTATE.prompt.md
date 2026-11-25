# Rewrite a coherent Dagster project from a RunMyJobs estate

You are writing ONE Dagster project for several RunMyJobs units from scratch. Use the estate view below to choose asset boundaries across units (a chain that runs another chain, a queue
shared by several chains, a definition used from two chains); then write each unit following its own prompt (`<unit>.prompt.md`) so each can be gated on its own.

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

## 2. The estate (facts; statements marked inferred are not facts)

# Estate: how the units relate

Facts from the extracted model, so a rewrite can choose asset boundaries across units. Lines marked *inferred* are suggestions to confirm, not facts.

## Units (4)

- `Forecast_Replenishment_Template` (chain): 16 call(s), converter disposition **degraded**, schedules: none, queues: none
- `Full_FR_Run` (chain): 10 call(s), converter disposition **degraded**, schedules: none, queues: none
- `Process_Negative_Stock_Template` (chain): 2 call(s), converter disposition **translated**, schedules: none, queues: none
- `ReplenishShop` (chain): 4 call(s), converter disposition **translated**, schedules: none, queues: none

## Chains calling chains

- `Forecast_Replenishment_Template` runs `Full_FR_Run`, `Process_Negative_Stock_Template`
- `Full_FR_Run` runs `ReplenishShop`
- top-level chains (nothing in the export calls them): Forecast_Replenishment_Template

## Shared queues

- none


## Shared definitions and outputs

- definition `System_DoNothing` is called from Forecast_Replenishment_Template, Full_FR_Run, Process_Negative_Stock_Template, ReplenishShop

## Schedules, events

- none

## Candidate asset boundaries (inferred)

- chain Forecast_Replenishment_Template runs chain(s) Full_FR_Run, Process_Negative_Stock_Template: either one pipeline whose assets are the kids' assets in order (asset dependencies across the units), or the parent as a thin orchestration job over kid pipelines that stay separate *inferred*
- chain Full_FR_Run runs chain(s) ReplenishShop: either one pipeline whose assets are the kids' assets in order (asset dependencies across the units), or the parent as a thin orchestration job over kid pipelines that stay separate *inferred*
- definition System_DoNothing is called from Forecast_Replenishment_Template, Full_FR_Run, Process_Negative_Stock_Template, ReplenishShop: one reusable function or component configured per caller, not copied *inferred*


## 3. The units

- `Forecast_Replenishment_Template` (chain): spec `forecast_replenishment_template.spec.md`, gates `runmyjobs-to-dagster accept rewrite/forecast_replenishment_template.spec.json ...`
- `Full_FR_Run` (chain): spec `full_fr_run.spec.md`, gates `runmyjobs-to-dagster accept rewrite/full_fr_run.spec.json ...`
- `Process_Negative_Stock_Template` (chain): spec `process_negative_stock_template.spec.md`, gates `runmyjobs-to-dagster accept rewrite/process_negative_stock_template.spec.json ...`
- `ReplenishShop` (chain): spec `replenishshop.spec.md`, gates `runmyjobs-to-dagster accept rewrite/replenishshop.spec.json ...`

## 4. How to proceed

1. Read `ESTATE.md` above and the units' specs. Decide the project's boundaries first: which chains stay separate pipelines, which become sub-pipelines whose assets the parent depends on, which
   definitions become one shared function or component configured per caller, which outputs are one asset refreshed by several units, which queues are one pool. Write the decision at the top of
   `src/rmj_rewrite_chains/defs/__init__.py` (a docstring) before writing code.
2. Write each unit under `src/rmj_rewrite_chains/defs/<unit_id>/` following the idioms below, then run its gates one unit at a time.
3. Share what is shared: a pool is named once per queue; a shared definition is written once; two units that refresh the same output depend on the same asset key.

## 5. Target idioms

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

## 6. Header docstrings

- The first lines of **every module you write** (and of `src/<pkg>/defs/<unit_slug>/__init__.py` when the unit is components-only YAML) are a docstring that accounts for every object under
  "Objects the header docstring must account for" in the spec: one line per object, `<id>: replaced by `<asset key>`` / `folded into `<asset key>`` / `merged into `<asset key>`` /
  `dropped: <reason>`; name only the replacing asset(s) or sensor(s) in backticks right after the verb and put details after a parenthesis or a semicolon (a later backticked name is read as another replacement); add `reimplemented` on a call whose command you did not keep verbatim. After that list: the registry components and tiers used, the pool commands, and every behavior you
  could not preserve or verify (stated, not hidden). A RunMyJobs object that is not accounted for fails the gate.

## 7. Acceptance

Each unit is gated on its own (`runmyjobs-to-dagster accept rewrite/<unit_id>.spec.json --project <project root> --pkg rmj_rewrite_chains --manifest migration_manifest.json`); the estate is accepted when
every unit you rewrote has passed and every unit you did not rewrite is left to the converter's output (the manifest's `route` field says which). A unit that fails stays deferred with the failure.
The converter's output (`runmyjobs-to-dagster convert ... --output components`) is the reference draft and the fallback for any unit; compare the two routes with `runmyjobs-to-dagster equivalence`.

## 8. Cost

The estate prompt is read once, then each unit costs a spec read, registry searches and gate iterations. Use it for a pilot or an application where one coherent, idiomatic project matters more than
a mechanical conversion.
