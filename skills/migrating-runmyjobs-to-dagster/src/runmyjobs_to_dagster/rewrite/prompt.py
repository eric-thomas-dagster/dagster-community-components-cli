"""Write `rewrite/<unit>.prompt.md` (and `ESTATE.prompt.md`): a self-contained prompt that asks Claude to write one unit's Dagster code from scratch, from its spec.

The prompt is deterministic text around the spec. It contains no secret value (the spec has none). It spends model effort per unit: use it where it earns it (`SKILL.md`, "Two routes").
"""
from __future__ import annotations

DCC = "skills/dagster-community-components/SKILL.md"

HIERARCHY = f"""1. Load the **`dagster-expert`** skill (by name) and follow its component hierarchy: a community registry component first, an official `dagster-<vendor>` integration next,
   hand-written code last. Create the project with `uvx create-dagster project <name> --uv-sync` if it does not exist (never by hand) and scaffold with `dg scaffold defs`. Check every Dagster
   name and signature with it, with `dg` (`dg list components --json`, `--help`) or with the installed source. Do not invent an API.
2. Load the **`dagster-community-components`** skill (by name; for agents that load skills from files: `{DCC}` in this repository, with `reference/cli.md`, `reference/catalog.md`,
   `reference/writing-yaml.md` and `reference/demo-workflow.md`). Its hard rules apply: never invent a component id, config field or CLI flag; search by capability and vendor before concluding
   nothing exists; read the real schema before writing any `defs.yaml`; registry component first, official integration second, hand-written code last.
3. Follow `reference/demo-workflow.md` of that skill for every stage of this unit: **search** (`dagster-component search "<stage keywords>" --json`), **verify** that stages chain (`produces` /
   `consumes`, the README's `Pairs with`), **read the schema** (`dagster-component info <id>`, `dagster-component schema <id> --format json`), **add and wire** (`dagster-component add <id>` or
   `type: dagster_community_components.<Class>` with `--as-package`, in a `defs.yaml` that uses only fields you read), **validate** (`dg check defs`, `dg list defs --json`).
   `dagster-component list --available` lists the registry (plain `list` shows what is installed).
4. For each registry component you use, say in the header docstring which id you used and which schema fields you set, and name its validation tier (`validation_level` in
   `dagster-component search --json`). A component that is not `live` carries a caveat sentence. If you use an official integration or hand-written code instead, say why no registry component fit."""

IDIOMS = """- **One asset per data product or job outcome.** A data product is a step whose output is persisted (a table, a file, a loaded fact) or consumed by more than the next step. Steps that only prepare
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
  right place in the graph, with its source kept in a comment. A body that returns success is never acceptable, and neither is silently leaving the step out."""

HEADER_RULE = """- The first lines of **every module you write** (and of `src/<pkg>/defs/<unit_slug>/__init__.py` when the unit is components-only YAML) are a docstring that accounts for every object under
  "Objects the header docstring must account for" in the spec: one line per object, `<id>: replaced by `<asset key>`` / `folded into `<asset key>`` / `merged into `<asset key>`` /
  `dropped: <reason>`; name only the replacing asset(s) or sensor(s) in backticks right after the verb and put details after a parenthesis or a semicolon (a later backticked name is read as another replacement); add `reimplemented` on a call whose command you did not keep verbatim. After that list: the registry components and tiers used, the pool commands, and every behavior you
  could not preserve or verify (stated, not hidden). A RunMyJobs object that is not accounted for fails the gate."""


def _gates_list(unit: str, unit_id: str, spec: dict) -> str:
    ex = spec["expected"]
    return f"""Your rewrite is accepted only when `runmyjobs-to-dagster accept rewrite/{unit_id}.spec.json --project <project root> --pkg <package> [--manifest migration_manifest.json]`
passes every gate (a gate that cannot run fails; it is never skipped silently):

1. **definitions load**: `dg check defs` passes in the project.
2. **definitions match the spec**: `dg list defs --json` and the loaded definitions show, for this unit: every RunMyJobs call accounted for in the header maps to an existing asset; each earlier-to-later
   pair in the spec's graph ({len(ex['order_pairs'])} here) is kept (the later asset is downstream of the earlier one, or both are folded into one asset); schedules equal the spec's
   (cron and time zone: {', '.join(f"`{s['cron']}` {s['time_zone']}" for s in ex['schedules']) or 'none'}); pools equal {', '.join(f'`{p}`' for p in ex['pools']) or 'none'}; retries match
   the spec's per-call retries on the asset that holds the call.
3. **commands preserved**: each command or SQL text in the spec ({len(ex['commands'])} here) appears in your code or YAML after normalising whitespace and parameter references, or its header line
   says `reimplemented`.
4. **completeness**: every object in the spec's accounting list ({len(spec['accounting'])} here) is named in a header docstring with what happened to it.
5. **generated pytest**: `tests/test_{unit_id}_rewrite.py` is written for you and run: the definitions load, the asset keys, dependencies, schedule and pools from the header are present, and, when the
   unit has no external system, the unit materializes with fake or local resources (a command that only echoes runs for real; SQL runs against a local SQLite URL). A unit with an external system
   (SAP, SMTP, a missing definition, a stub) is checked for shape only and the gate says why; there is no fake pass.
6. **structural asset checks** exist where the spec's facts support them (an output the converter derived a check for), and pass when materialized.
7. **no secrets**: a scan of your code, YAML and tests finds no credential-looking literal.
8. **no invented APIs**: every import resolves and every component `type:` is an importable class.

A rewrite that fails a gate stays **deferred** with the failure recorded; fix the cause and run the gates again. The manifest records `route: rewrite` and the gate result for every unit this spec
covers ({', '.join(spec['manifest_units']) or 'none'}). Never mark a unit complete yourself and never edit the gate results."""


def render_unit(spec: dict, spec_md: str, project: str = "my_project", package: str = "my_pkg", converter_dir: str | None = None) -> str:
    u, uid = spec["unit"], spec["unit_id"]
    cv = spec["converter"]
    where = (f"The converter's output for this unit is under `{converter_dir}` (disposition `{cv['disposition']}`; stubbed: {', '.join(cv['stubbed']) or 'none'}; "
             f"degraded: {', '.join(cv['degraded']) or 'none'}). Read it as a reference draft: it shows the source translated one call at a time. Do not copy its structure; use it to check "
             "that you have not lost behavior." if converter_dir else
             f"No converter output path is given (disposition `{cv['disposition']}`; stubbed: {', '.join(cv['stubbed']) or 'none'}; degraded: {', '.join(cv['degraded']) or 'none'}). Work from the spec.")
    return f"""# Rewrite RunMyJobs {spec['kind'].replace('_', ' ')} `{u}` as idiomatic Dagster

You are writing the Dagster code for one RunMyJobs unit from scratch. Do not transform the RunMyJobs objects one for one: understand what the unit does from the specification below, then build what
a Dagster engineer would build for that behavior. The converter's output is a reference draft and the fallback if your rewrite fails the gates; it is not the model to imitate.

## 1. Load the skills and follow them

{HIERARCHY}

## 2. The specification (facts; statements marked inferred are not facts)

{spec_md}

## 3. What to build: the target idioms

{IDIOMS}
- Preserve every behavior under "must be preserved" in the spec; the "acceptable differences" are yours to use.

## 4. Where to write it

- The unit's definitions go under `src/{package}/defs/{uid}/` or in `src/{package}/defs/{uid}.py`. A folder that holds a `defs.yaml` does not load Python modules next to it: put registry-component YAML in `{uid}/components/defs.yaml` and hand-written modules in `{uid}/python/` (with an `__init__.py` in each), and the header docstring in `{uid}/__init__.py`. Define a job named
  `{uid}` over the unit's assets (components output defines jobs through the schedule component; a hand-written module defines it with `dg.define_asset_job`) and the schedule(s) from the spec.
{HEADER_RULE}
- Secrets: environment variables and resources only.

## 5. The acceptance gates

{_gates_list(u, uid, spec)}

## 6. The converter's output (reference and fallback)

{where}

If the rewrite cannot pass the gates, the converter's output is what ships (a stubbed call stays pending or deferred as the converter left it). Say plainly in your final message what you could not
preserve or verify.

## 7. Cost

This route spends model effort on every unit (reading the spec, searching the registry, writing and iterating until the gates pass). Use it where idiomatic output matters or the converter stubs or
degrades the unit; the converter stays the default for what it translates cleanly.
"""


def render_estate(estate: dict, estate_md: str, specs: dict[str, dict], project: str = "my_project", package: str = "my_pkg") -> str:
    units = estate["units"]
    lines = "\n".join(f"- `{u}` ({estate['unit_kinds'][u]}): spec `{specs[u]['unit_id']}.spec.md`, gates `runmyjobs-to-dagster accept rewrite/{specs[u]['unit_id']}.spec.json ...`" for u in units)
    return f"""# Rewrite a coherent Dagster project from a RunMyJobs estate

You are writing ONE Dagster project for several RunMyJobs units from scratch. Use the estate view below to choose asset boundaries across units (a chain that runs another chain, a queue
shared by several chains, a definition used from two chains); then write each unit following its own prompt (`<unit>.prompt.md`) so each can be gated on its own.

## 1. Load the skills and follow them

{HIERARCHY}

## 2. The estate (facts; statements marked inferred are not facts)

{estate_md}

## 3. The units

{lines}

## 4. How to proceed

1. Read `ESTATE.md` above and the units' specs. Decide the project's boundaries first: which chains stay separate pipelines, which become sub-pipelines whose assets the parent depends on, which
   definitions become one shared function or component configured per caller, which outputs are one asset refreshed by several units, which queues are one pool. Write the decision at the top of
   `src/{package}/defs/__init__.py` (a docstring) before writing code.
2. Write each unit under `src/{package}/defs/<unit_id>/` following the idioms below, then run its gates one unit at a time.
3. Share what is shared: a pool is named once per queue; a shared definition is written once; two units that refresh the same output depend on the same asset key.

## 5. Target idioms

{IDIOMS}

## 6. Header docstrings

{HEADER_RULE}

## 7. Acceptance

Each unit is gated on its own (`runmyjobs-to-dagster accept rewrite/<unit_id>.spec.json --project <project root> --pkg {package} --manifest migration_manifest.json`); the estate is accepted when
every unit you rewrote has passed and every unit you did not rewrite is left to the converter's output (the manifest's `route` field says which). A unit that fails stays deferred with the failure.
The converter's output (`runmyjobs-to-dagster convert ... --output components`) is the reference draft and the fallback for any unit; compare the two routes with `runmyjobs-to-dagster equivalence`.

## 8. Cost

The estate prompt is read once, then each unit costs a spec read, registry searches and gate iterations. Use it for a pilot or an application where one coherent, idiomatic project matters more than
a mechanical conversion.
"""
