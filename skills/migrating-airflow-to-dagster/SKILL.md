---
name: migrating-airflow-to-dagster
description: Guide for migrating Apache Airflow DAGs to Dagster. Use when the user mentions migrating, converting, porting or moving off Airflow (or MWAA, Cloud Composer, Astronomer) to Dagster, wants to assess such a migration, or asks what an Airflow construct maps to in Dagster. Covers DAGs, schedules, TaskFlow tasks, operators, sensors, datasets/assets, pools, XCom, Variables/Connections, dynamic task mapping, dag-factory and Cosmos. Load dagster-expert alongside this skill for Dagster specifics.
---

# Airflow to Dagster migration

Migrate honestly and incrementally. Airflow stays authoritative until parity is shown, every DAG and task gets an explicit disposition, and semantic differences are documented, never papered over.

A deterministic scanner enumerates the Airflow project (so completeness does not depend on you); you classify, translate and validate.

## Pick a tier first

There are three ways in, and one project often uses more than one. See `reference/tiers.md` for details.

| Tier | What it is | Use when |
|---|---|---|
| 1. Observe | Dagster reads Airflow's run history (Airlift peer/observe) and shows DAGs and tasks as assets | You want visibility now with zero change to Airflow |
| 2. Bridge | Existing DAG code keeps running, orchestrated by Dagster (Airlift proxying, or the script-orchestrator component) | You need to move scheduling off Airflow before rewriting the code |
| 3. Native | DAGs are rewritten as Dagster assets, components, schedules and sensors | You want the asset model, lineage, and to retire the Airflow code |

The scanner, manifest and gates below are for tier 3 and for deciding what stays in tiers 1-2.

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

For the long tail of operators with no official integration, search the community registry (`dagster-component search "<term>" --json`) before writing anything.

**An Airflow skill (optional, source side).** Astronomer publishes Apache-2.0 skills in `astronomer/agents` (https://github.com/astronomer/agents). The `airflow` skill drives a live instance through an `af` CLI. It is a third-party dependency: use it when present, and fall back to the static scan and a person when it is not. It earns its place in these steps:

| Phase | Use it for |
|---|---|
| 1 Inventory | `af dags list` is the ground truth the static scan is checked against: the DAG counts must agree, or the difference is a finding (factory-generated DAGs). `af dags errors` should line up with `unparsed` units. `af dags source <dag_id>` retrieves code when the repo is unavailable (MWAA, Composer, Astro). |
| 5 Platform layer | Listing Connections, Variables and Pools to build the secrets and pool map. |
| 6 Parity | `af runs list` and `af dags stats` for durations and failure rates to compare against Dagster; `testing-dags` to run the source DAG and capture baseline outputs. |
| 6 Cutover | `af dags pause` / `unpause`. These change production: only with the user's explicit approval, one domain at a time. Everything else above is read-only. |

The `migrating-airflow-2-to-3` skill is only needed when the source is Airflow 2.x and the user also wants it upgraded first. It is not a prerequisite for migrating to Dagster.

## Hard rules

1. **Never stub.** A unit is either translated and passing its gate, or deferred with a written reason. A body that just returns success is a failure.
2. **No silent omissions.** Every manifest unit ends `complete` or `deferred (reason)`. `scripts/status.py summary` exits nonzero otherwise; run it before claiming done.
3. **Equivalence rows for every trigger.** Each schedule, sensor and dataset trigger gets a report row: Airflow spelling, Dagster spelling, and the behavioral delta in one sentence (catchup, data interval versus partition, trigger rules). The sin is not the delta, it is the undocumented delta.
4. **Fix classes, not instances.** If a translation pattern fails the gate, fix the pattern (record it in your migration notes), then re-apply; do not hand-patch one unit.
5. **Do not invent APIs.** Check names and signatures with `dagster-expert`, `dg` (`dg list components`, `--help`) and the registry (`dagster-component search "<term>" --json`) before relying on them. Never claim "no component exists" without searching.

## Workflow

### Phase 0: Preflight

Load `dagster-expert` (and the Airflow skill if the user has one). Confirm the Airflow version (2.x or 3.x), how DAGs are defined (plain Python, TaskFlow, dag-factory YAML, Cosmos), where they run (self-managed, MWAA, Composer, Astro), the target Dagster setup (project layout, Dagster+ or open source), and repo access. Baseline the Airflow test suite; record pre-existing failures so they are not blamed on the migration.

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

### Phase 1.5: Go/no-go

Before translating, answer what this team gives up and whether each loss has an acceptable answer. Typical items: breadth of provider operators (mitigation: registry components, official integrations, Pipes), XCom-based data passing (mitigation: explicit storage or IO managers), dynamic task mapping (dynamic partitions or a graph), Airflow plugins and custom UI, and team familiarity. Outcomes: migrate, migrate with conditions (the common case; record the conditions), or not yet (name the unmitigated losses). A guide that cannot say "not yet" cannot be trusted when it says "go".

### Phase 2: Plan

- Choose the tier per DAG (see `reference/tiers.md`).
- Decide asset granularity: one Dagster asset per data product the DAG produces, not per task. Tasks that only prepare, wait or notify usually fold into an asset, a sensor or an automation condition.
- Write each unit's `target` into the manifest: assets, deps, jobs, schedules (`validate_defs.py` documents the shape). A unit with no target is reported as UNPLANNED.
- Decide the platform mapping (Phase 5) up front: Variables and Connections to env vars, secrets and resources.

### Phase 3: Trial

Migrate two or three representative DAGs end to end through the gates before fanning out: one plain scheduled DAG, one with a sensor or dataset trigger, one with XCom or branching. What the trial teaches updates your mapping rules before you scale. `examples/three_dags/` is a completed trial (plain scheduled DAG, dataset producer and consumer) with the source, the Dagster result, the manifest and the equivalence rows: read it as the nearby already-migrated example.

### Phase 4: Migrate, domain by domain

Per unit: `pending` → translate → `dg check defs` → validate → `complete`, or `deferred` with a reason. Use `dagster-expert` for project layout (`dg scaffold defs`), components and automation. Prefer registry components for provider operators.

```
python3 scripts/validate_defs.py <dagster_project> --manifest manifest.json --advance
python3 scripts/status.py advance manifest.json A012 --evidence "ported; validate passes; parity checked on 2024-xx"
python3 scripts/status.py defer   manifest.json A031 --reason "custom timetable; tracked separately"
```

`--advance` only marks MECH units that load and match their plan; it does not prove behavior. If a gate fails, retry with the validator output in context (cap around ten attempts, then defer with the failure class).

### Phase 5: Platform layer

Variables to environment variables or secrets (`dg plus create env`, never in the repo), Connections to resources, pools to Dagster pools (limits configured in Dagster), `default_args` retries to `RetryPolicy`, alerts and callbacks to alert policies or failure sensors, deployment to Dagster+ (see the CI/CD walkthrough in this repo's `examples/`).

### Phase 6: Side by side, then cutover

Run Dagster alongside Airflow on the same logical window with the Dagster schedule off or targeting a non-production sink. Compare outputs (row counts and checksums), durations and failure behavior. Cut over per domain in a change window: pause the Airflow DAG, enable the Dagster schedule or automation; rollback is the reverse. Keep Airflow's run history readable afterward; it does not migrate.

### Phase 7: Report

`scripts/status.py summary` must pass. The report states the go/no-go outcome, every disposition, every equivalence row, the losses plainly, the secrets map, and the deferred list with reasons. Spot-check ten `complete` claims before delivering it.

## Reference

| Need | Read |
|---|---|
| What maps to what, and how sure we are | `reference/mapping.md` |
| Parity checks that justify advancing judgment units | `reference/parity.md` |
| A completed worked conversion | `examples/three_dags/MIGRATION.md` |
| Observe, bridge, native; Airlift and the script-orchestrator component | `reference/tiers.md` |

## Scripts

| Script | Purpose |
|---|---|
| `scripts/inventory.py` | Scan the Airflow repo into a manifest (read-only) |
| `scripts/airflow_truth.py` | Dump Airflow's own DAG and task lists (executes DAG code; sandbox only) |
| `scripts/reconcile.py` | Merge that runtime view into the manifest so nothing is omitted |
| `scripts/validate_defs.py` | Gates: definitions load and match each unit's plan |
| `scripts/status.py` | Per-unit state machine and the completeness gate |
