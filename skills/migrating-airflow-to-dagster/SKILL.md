---
name: migrating-airflow-to-dagster
description: Guide for migrating Apache Airflow DAGs to Dagster. Use when the user mentions migrating, converting, porting or moving off Airflow (or MWAA, Cloud Composer, Astronomer) to Dagster, wants to assess such a migration, or asks what an Airflow construct maps to in Dagster. Covers DAGs, schedules, TaskFlow tasks, operators, sensors, datasets/assets, pools, XCom, dynamic task mapping and dag-factory, with a converter that writes Dagster code for them. Connections become resources only for the operators the converter handles, Variables are read from `AIRFLOW_VAR_*` environment variables, and Astronomer Cosmos DAGs (`DbtDag`, `DbtTaskGroup`, the dbt operators) convert to Dagster's dbt component. Also writes a wave plan before conversion and a per-DAG cutover checklist after it, and has concrete notes for Airlift and for hosted Airflow (MWAA, Composer, Astro). Load dagster-expert alongside this skill for Dagster specifics.
---

# Airflow to Dagster migration

Migrate honestly and incrementally. Airflow stays authoritative until parity is shown, every DAG and task gets an explicit disposition, and semantic differences are documented, never papered over.

A deterministic scanner enumerates the Airflow project (so completeness does not depend on you); you classify, translate and validate.

## Pick a tier first

There are three ways in, and one project often uses more than one. See `reference/tiers.md` for details.

| Tier | What it is | Use when |
|---|---|---|
| 1. Observe | Dagster reads Airflow's run history (Airlift peer/observe) and shows DAGs and tasks as assets | You want visibility now with zero change to Airflow (Airflow 2.x only: see `reference/airlift.md`) |
| 2. Bridge | Existing DAG code keeps running, orchestrated by Dagster (Airlift proxying, `reference/airlift.md`, or the script-orchestrator component) | You need to move scheduling off Airflow before rewriting the code |
| 3. Native | DAGs are rewritten as Dagster assets, components, schedules and sensors | You want the asset model, lineage, and to retire the Airflow code |

The tiers are **where the work runs**: 1 observe, 2 bridge (Airlift keeps Airflow running), 3 native (Dagster owns the code). Inside tier 3 there are two **routes** for **how the code gets written**:

| Route | What it is | Choose it for |
|---|---|---|
| **convert** | the deterministic converter (`convert_ir.py`): a near like-for-like conversion of what Airflow built, task by task (or `--granularity product`) | the mechanical bulk; it is the default |
| **rewrite** | a generated spec (`dag_spec.py`) and prompt (`rewrite_prompt.py`): Claude writes the Dagster code from scratch with `dagster-expert` and `dagster-community-components`, accepted by `accept_rewrite.py` through the same kind of gates | DAGs the converter deferred or stubbed, redesign-heavy DAGs, pilot DAGs where idiomatic output matters, anything `plan.py` puts in the last wave |

The rewrite is expected to be more idiomatic (one asset per data product, glue folded, registry components) and to cover operators the converter stubs; the evidence so far is `examples/rewrite/` (two worked DAGs and a sample of eight) and `reference/rewrite-route.md`, not a claim that it is better. The converter's output is the reference draft and the fallback for every rewrite. The rewrite route spends model effort on every DAG.

The scanner, manifest and gates below are for tier 3 and for deciding what stays in tiers 1-2. **Airflow 3 source:** `dagster-airlift` 0.29.31 cannot read an Airflow 3 REST API (`/api/v2`), so tier 3 with the converter is the route; the extractor runs the DAG files and does not use the REST API (`reference/airlift.md`).

## Companion skills

This skill covers the migration method. It does not duplicate Dagster or Airflow reference material; load these alongside it.

**`dagster-expert` (required).** The authority for the target side. Use it instead of recalling Dagster APIs from memory (hard rule 5), and create the target project with `uvx create-dagster project <name> --uv-sync`, never by hand.

| Phase or construct | `dagster-expert` reference to read |
|---|---|
| New project, layout | `references/cli/create-dagster.md`, `references/cli/scaffold/defs.md` |
| Assets, deps, metadata, partitions (tasks, datasets, `catchup`, `.expand`) | `references/assets/INDEX.md` and its pages |
| Schedules, timezones | `references/automation/schedules.md` |
| Dataset triggers, sensors, `depends_on_past`, cross-DAG waits | `references/automation/choosing-automation.md`, `references/automation/declarative-automation/INDEX.md`, `references/automation/sensors/` |
| Provider operators (Snowflake, S3, dbt, Spark, ...) | `references/integrations/INDEX.md`, then the registry search below |
| Variables, Connections, secrets | `references/env-vars.md`, `references/cli/plus/INDEX.md` |
| Gates (`dg check defs`, `dg list defs --json`, `dg launch`) | `references/cli/check.md`, `references/cli/list-defs.md`, `references/cli/launch.md` |
| Components for repeated DAG shapes (dag-factory) | `references/components/` |

For the long tail of operators with no official integration, search the component registry (`dagster-component search "<term>" --json`) before writing anything.

**An Airflow skill (optional, source side).** Astronomer publishes Apache-2.0 skills in `astronomer/agents` (https://github.com/astronomer/agents). The `airflow` skill drives a live instance through an `af` CLI. It is a third-party dependency: use it when present, and fall back to the static scan and a person when it is not. It earns its place in these steps:

| Phase | Use it for |
|---|---|
| 1 Inventory | `af dags list` is the ground truth the static scan is checked against: the DAG counts must agree, or the difference is a finding (factory-generated DAGs). `af dags errors` should line up with `unparsed` units. `af dags source <dag_id>` retrieves code when the repo is unavailable (MWAA, Composer, Astro). |
| 5 Platform layer | Listing Connections, Variables and Pools to build the secrets and pool map. |
| 6 Parity | `af runs list` and `af dags stats` for durations and failure rates to compare against Dagster; `testing-dags` to run the source DAG and capture baseline outputs. |
| 6 Cutover | `af dags pause` / `unpause`. These change production: only with the user's explicit approval, one domain at a time. Everything else above is read-only. |

The `migrating-airflow-2-to-3` skill is only needed when the source is Airflow 2.x and the user also wants it upgraded first. It is not a prerequisite for migrating to Dagster.

## Hard rules

1. **Stubs must be loud.** A unit is either translated and passing its gate, or deferred with a written reason. A body that just returns success is a failure. The one allowed stub is the converter's marked caveat: a task it cannot translate (Airflow API call, unsupported operator or template) keeps its place in the DAG as an asset whose body raises `AIRFLOW-CAVEAT: <reason>` when it runs (`--caveats skip` returns nothing instead). The unit stays `deferred` with that reason, the report lists it under `caveats`, and a DAG with caveats counts as converted only for its shape; it passes parity only if Airflow and Dagster produce the same outcomes.
2. **No silent omissions.** Every manifest unit ends `complete` or `deferred (reason)`. `scripts/status.py summary` exits nonzero otherwise; run it before claiming done.
3. **Equivalence rows for every trigger.** Each schedule, sensor and dataset trigger gets a report row: Airflow spelling, Dagster spelling, and the behavioral delta in one sentence (catchup, data interval versus partition, trigger rules). The sin is not the delta, it is the undocumented delta.
4. **Fix classes, not instances.** If a translation pattern fails the gate, fix the pattern (record it in your migration notes), then re-apply; do not hand-patch one unit.
5. **Do not invent APIs.** Check names and signatures with `dagster-expert`, `dg` (`dg list components`, `--help`) and the registry (`dagster-component search "<term>" --json`) before relying on them. Never claim "no component exists" without searching.

## Workflow

### Phase 0: Preflight

Load `dagster-expert` (and the Airflow skill if the user has one). Confirm the Airflow version (2.x or 3.x), how DAGs are defined (plain Python, TaskFlow, dag-factory YAML, Cosmos), where they run (self-managed, MWAA, Composer, Astro: `reference/hosted-airflow.md` says how to get the DAG files, the Airflow version, the packages and the Connection and Variable names from each), the target Dagster setup (project layout, Dagster+ or open source), and repo access. Baseline the Airflow test suite; record pre-existing failures so they are not blamed on the migration.

### Phase 1: Inventory (read-only)

```
python3 scripts/inventory.py <airflow_repo> --out manifest.json
```

Every DAG and task becomes a unit with file and line, schedule, signals (XCom, Variables, Connections, Jinja, trigger rules, dynamic mapping, datasets) and a suggested class: MECH (mechanical), JUDG (judgment), REDESIGN (needs a design decision). The scanner never imports DAG code, so it is safe on untrusted repos. It reports `unparsed` units for files it could not read; treat those as findings.

A static scan cannot see everything (factories, loops, constructs it does not model), so close the gap with Airflow's own view. Either compare against `af dags list`, or, in a sandbox with Airflow and the project's providers installed and code you trust, run:

```
python3 scripts/airflow_truth.py <dags_folder> --out truth.json     # EXECUTES the DAG files, like Airflow's parser
python3 scripts/reconcile.py manifest.json truth.json
```

`reconcile.py` adds a pending JUDG unit for every DAG or task only Airflow knows about, so nothing is omitted silently, and it lists static DAGs Airflow did not produce (usually files that failed to import; `truth.json` records the import errors). Confirm or change each suggested class using `reference/mapping.md`. A construct with no row in the mapping is itself a finding: record it, do not guess.

### Phase 1.25: Plan in waves

```
python3 scripts/plan.py manifest.json [--ir ir.json] [--report report.json] --out-dir plan
```

Writes `plan.md` and `plan.json`: per DAG a pattern (dbt, ELT, Python ETL, SQL, containers/Spark, sensor-driven, ops), a complexity bucket from this skill's own MECH / JUDG / REDESIGN classes, a path (native rewrite, observe/bridge with airlift, defer), a wave (1 = mechanical with high lineage value such as dbt and ELT; the last wave = redesign-heavy), a coarse effort with the formula printed in the file, and a suggested pilot set for Phase 3. Deterministic, and **every number is a heuristic**: use it to order the work and start the scope conversation, not as a quote. Run it again with `--ir` and `--report` once the converter has run; a DAG converted with no stub drops to review-only effort. See `examples/migration_plan/`.

### Phase 1.5: Go/no-go

Before translating, answer what this team gives up and whether each loss has an acceptable answer. Typical items: breadth of provider operators (mitigation: registry components, official integrations, Pipes), XCom-based data passing (mitigation: explicit storage or IO managers), dynamic task mapping (dynamic partitions or a graph), Airflow plugins and custom UI, and team familiarity. Outcomes: migrate, migrate with conditions (the common case; record the conditions), or not yet (name the unmitigated losses). A guide that cannot say "not yet" cannot be trusted when it says "go".

### Phase 2: Plan

- Choose the tier per DAG (see `reference/tiers.md`); the wave plan from `scripts/plan.py` (Phase 1.25) is the first cut, to be confirmed or changed per DAG.
- Decide asset granularity: one Dagster asset per data product the DAG produces, not per task. Tasks that only prepare, wait or notify usually fold into an asset, a sensor or an automation condition.
- Write each unit's `target` into the manifest: assets, deps, jobs, schedules (`validate_defs.py` documents the shape). A unit with no target is reported as UNPLANNED.
- Decide the platform mapping (Phase 5) up front: Variables and Connections to env vars, secrets and resources.

### Phase 3: Trial

Migrate two or three representative DAGs end to end through the gates before fanning out: one plain scheduled DAG, one with a sensor or dataset trigger, one with XCom or branching. What the trial teaches updates your mapping rules before you scale. `examples/three_dags/` is a completed trial (plain scheduled DAG, dataset producer and consumer) with the source, the Dagster result, the manifest and the equivalence rows: read it as the nearby already-migrated example.

### Phase 4: Migrate, domain by domain

**When to use the rewrite route.** Use it for a DAG when the converter deferred it or left a stub in it, when it is redesign-heavy (branching, dynamic mapping, custom timetables) or in the plan's last wave, and for the pilot DAGs where idiomatic output matters while the mapping is being learned. Keep the converter for the mechanical bulk. To rewrite a DAG:

```
python3 scripts/dag_spec.py ir.json --report report.json --manifest manifest.json [--parity af_results.json] --out-dir rewrite   # facts only: rewrite/<dag>.spec.{md,json}
python3 scripts/rewrite_prompt.py rewrite/<dag>.spec.json --converter-module <the converter's module> --project <name> --package <pkg>   # rewrite/<dag>.prompt.md
# give the prompt to Claude (with dagster-expert and dagster-community-components loaded); it writes src/<pkg>/defs/<dag>.py
python3 scripts/accept_rewrite.py --spec rewrite/<dag>.spec.json --module <pkg>.defs.<dag> --project <root> [--parity af_results.json] --manifest manifest.json
```

The gates are `dg check defs`, a generated pytest, asset checks present, parity where there is an Airflow baseline, no secret values, no invented APIs, and every Airflow task accounted for in the header. The manifest records `route: rewrite` and the gate results for each unit; a rewrite that fails stays deferred with the failure and the converter output remains the fallback. Never mark a rewritten unit complete without the gates.

**Convert the mechanical units by code, first.** Run `airflow_ir.py` then `convert_ir.py` (see `reference/conversion.md`): every DAG built from Python, Bash, S3, Snowflake, EmptyOperator and the provider operators listed in `reference/conversion.md` (HTTP, SQL, Kubernetes pods, email, Slack webhook, dbt Cloud, Airbyte, Fivetran, Databricks) tasks, cron or asset-triggered, converts without hand work (the provider mappings that were not run against a live service carry an UNVERIFIED caveat: confirm them in the side-by-side run), and each unconverted DAG comes with the reason and the integration or component that fits. Cosmos DAGs (`DbtDag`, `DbtTaskGroup`, `DbtRunLocalOperator` and the others) convert to one `dagster_dbt.DbtProjectComponent` per group, with per-model assets, asset checks and the surrounding tasks kept as asset dependencies; the dbt project itself is copied by you (the report says where). Every Cosmos execution mode (local, virtualenv and the container modes) converts the same way, because dbt runs in the code location's environment in Dagster; the dbt project is used unchanged; a container that runs something other than dbt stays a loud stub. Extracting the IR needs `astronomer-cosmos` and a dbt that can render the project in the extraction environment (`reference/conversion.md`, "Astronomer Cosmos"). `--granularity product` (opt-in) folds steps into the asset they feed, one asset per data product (`reference/conversion.md`, "Choosing granularity"; the default stays one asset per task). Each converted DAG also gets a generated `tests/test_<dag>.py` and, where the source shows something checkable, non-blocking structural asset checks (`reference/conversion.md`, "Generated tests and structural asset checks"); run them, then run the parity harness on the converted DAGs (`examples/runtime_conversion/`): loading proves nothing about behavior. Then hand-convert what the report defers, using the other references.


Per unit: `pending` → translate → `dg check defs` → validate → `complete`, or `deferred` with a reason. Use `dagster-expert` for project layout (`dg scaffold defs`), components and automation. Prefer registry components for provider operators.

```
python3 scripts/validate_defs.py <dagster_project> --manifest manifest.json --advance
python3 scripts/status.py advance manifest.json A012 --evidence "ported; validate passes; parity checked on 2024-xx"
python3 scripts/status.py defer   manifest.json A031 --reason "custom timetable; tracked separately"
```

`--advance` only marks MECH units that load and match their plan; it does not prove behavior. If a gate fails, retry with the validator output in context (cap around ten attempts, then defer with the failure class).

### Phase 5: Platform layer

Connections: `scripts/connections_to_resources.py connections.json` turns `airflow connections export --format json` (or the names-only list) into a resources section and a `.env.example` with variable names only, and never reads a secret value (`reference/conversion.md`, "Connections export"); then variables to environment variables or secrets (`dg plus create env`, never in the repo), Connections to resources, pools to Dagster pools (limits configured in Dagster), `default_args` retries to `RetryPolicy`, alerts and callbacks to alert policies or failure sensors, deployment to Dagster+ (see the CI/CD walkthrough in this repo's `examples/`).

### Phase 6: Side by side, then cutover

Run Dagster alongside Airflow on the same logical window with the Dagster schedule off or targeting a non-production sink. Compare outputs (row counts and checksums), durations and failure behavior. Cut over per domain in a change window: pause the Airflow DAG, enable the Dagster schedule or automation; rollback is the reverse. Keep Airflow's run history readable afterward; it does not migrate.

```
python3 scripts/cutover.py manifest.json report.json --ir ir.json [--parity-state parity_state/] --out-dir cutover
```

Writes `cutover-<dag_id>.md` for each converted DAG (pause plan, catchup and backfill decision, secrets and connections by name, schedule and timezone check, limits to apply, what was NOT converted and needs a manual replacement, freshness, parity evidence, on-call handoff, rollback, decommission) and prints the commands for a person to run. **It never pauses, starts, stops or deletes anything.** Do not cut over a DAG with an open blocker or an unreplaced alert (`reference/cutover-checklist.md`).

### Phase 7: Report

`scripts/status.py summary` must pass. The report states the go/no-go outcome, every disposition, every equivalence row, the losses plainly, the secrets map, and the deferred list with reasons. Spot-check ten `complete` claims before delivering it.

## Reference

| Need | Read |
|---|---|
| What maps to what, and how sure we are | `reference/mapping.md` |
| Parity checks that justify advancing judgment units, and running them incrementally (`scripts/parity_incremental.py`) | `reference/parity.md` |
| DAG factories, Blueprint, dag-factory, loops that generate DAGs | `reference/factories.md` |
| Dynamic task mapping (`.expand`): static and dynamic partitions | `reference/dynamic-mapping.md` |
| Converting the mechanical DAGs by code generation from the runtime IR, and where existing components fit | `reference/conversion.md` |
| A converted repo with the parity harness | `examples/runtime_conversion/` |
| Which component and integration enhancements would convert more DAGs, ranked by measured impact | `reference/component-wishlist.md` |
| Compatibility across Airflow releases (2.2 to 3.4): what was tested, results, what changes by release | `reference/compatibility.md` |
| Partitioned timetables (`CronPartitionTimetable`, `PartitionedAssetTimetable`, partition mappers) and what converts | `reference/partitions.md` |
| Identical DAGs differing in one argument, converted to one partitioned asset | `examples/dagfactory_family/` |
| A worked factory conversion (six factories to one component) | `examples/s3_to_snowflake_factories/MIGRATION.md` |
| A completed worked conversion | `examples/three_dags/MIGRATION.md` |
| Observe, bridge, native; Airlift and the script-orchestrator component | `reference/tiers.md` |
| Airlift in detail: the component YAML, task mappings, proxying and rollback, authentication for MWAA, Composer and Astro, the Airflow 3 limitation, what was verified | `reference/airlift.md` |
| MWAA, Cloud Composer, Astro: DAG files, Airflow version, packages, REST API access, Connection and Variable names, limits | `reference/hosted-airflow.md` |
| The cutover checklist and what each section is for | `reference/cutover-checklist.md` |
| A worked wave plan and cutover checklists | `examples/migration_plan/` |
| The rewrite route on two DAGs (one the converter handles, one it stubs), with the spec, prompt and gates | `examples/rewrite/`, `reference/rewrite-route.md` |

## Scripts

| Script | Purpose |
|---|---|
| `scripts/inventory.py` | Scan the Airflow repo into a manifest (read-only) |
| `scripts/airflow_ir.py` | Extract every DAG as Airflow built it (expanded loops, factories, resolved arguments, callable source) into an IR (executes DAG code; sandbox only) |
| `scripts/convert_ir.py` | Generate Dagster code for the mechanical constructs; defer everything else with the reason |
| `scripts/convert_ir.py --output components` | The same conversion, writing registry components (YAML) for DAGs whose tasks all have one (see `reference/conversion.md`) |
| `scripts/dag_spec.py` | Facts-only specification of each DAG from the IR (rewrite route); no secret value |
| `scripts/rewrite_prompt.py` | The self-contained prompt that has Claude write one DAG from its spec (rewrite route) |
| `scripts/run_evals.py` | Skill-level evals (`evals/`): fixtures converted, then `dg check defs`, the generated tests, no invented component types and no secret values checked |
| `scripts/accept_rewrite.py` | The gates for a rewrite, and the manifest record of which route produced each unit |
| `scripts/plan.py` | Wave plan from the manifest (and IR and report): pattern, complexity, path, wave, coarse effort, pilot; heuristic and deterministic |
| `scripts/cosmos_ops.py` | The Cosmos mapping `convert_ir.py` uses (dbt component, dbt steps, profiles) |
| `scripts/cutover.py` | One cutover checklist per converted DAG; writes markdown and prints commands, acts on nothing |
| `scripts/apply_conversion.py` | Record the converter's outcome in the manifest (plans and deferrals) |
| `scripts/airflow_truth.py` | Dump Airflow's own DAG and task lists (executes DAG code; sandbox only) |
| `scripts/reconcile.py` | Merge that runtime view into the manifest so nothing is omitted |
| `scripts/validate_defs.py` | Gates: definitions load and match each unit's plan |
| `scripts/status.py` | Per-unit state machine and the completeness gate |
