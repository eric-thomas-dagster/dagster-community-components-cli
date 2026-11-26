#!/usr/bin/env python3
"""Write `rewrite/<dag_id>.prompt.md`: a self-contained prompt that asks Claude to write one DAG's Dagster code from scratch, from the spec `dag_spec.py` wrote.

    python3 rewrite_prompt.py rewrite/<dag>.spec.json [--converter-module path/to/defs/<dag>.py] [--project <name>] [--package <pkg>] [--out-dir rewrite]
    python3 rewrite_prompt.py rewrite/ ...                       (every *.spec.json in the folder)

This is the second route inside tier 3 (Dagster owns the code): the converter (`convert_ir.py`) transforms the source almost like for like; the rewrite route builds a thorough understanding of
what the DAG does and has Claude, with the `dagster-expert` and `dagster-community-components` skills and the component registry, write idiomatic Dagster from that. The converter's output is the
reference draft and the fallback. The prompt is deterministic text around the spec; it contains no secret value (the spec has none). It spends model effort per DAG: use it where it earns it
(`SKILL.md`, "When to use the rewrite route").
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import rewrite_patterns

DCC = "skills/dagster-community-components/SKILL.md"


def render(spec: dict, spec_md: str, converter_module: str | None, project: str, package: str) -> str:
    d = spec["dag_id"]
    cv = spec.get("converter") or {}
    ref = (f"The converter's output for this DAG is at `{converter_module}` (status `{cv.get('status')}`; stubbed tasks: {', '.join(cv.get('stubbed') or []) or 'none'}). Read it as a draft: it shows the "
           "source's behaviour translated one task at a time. Do not copy its structure; use it to check that you have not lost behaviour." if converter_module else
           f"No converter output is given (converter status: `{cv.get('status')}`" + (f", reason: {cv.get('reason')}" if cv.get("reason") else "") + "). Work from the spec alone.")
    pats = rewrite_patterns.render(rewrite_patterns.detect(spec))
    patterns = (pats + "\n") if pats else ""
    return f"""# Rewrite Airflow DAG `{d}` as idiomatic Dagster

You are writing the Dagster code for one Airflow DAG from scratch. Do not transform the Airflow source line by line: understand what the DAG does from the specification below, then build what a Dagster
engineer would build for that behaviour. The converter's output is a draft and the fallback if your rewrite fails the gates; it is not the model to imitate.

## 1. Load the skills and follow them

1. Load the **`dagster-expert`** skill (by name) and follow its component hierarchy: components and registry/official integrations before hand-written code; create the project with
   `uvx create-dagster project {project} --uv-sync` if it does not exist, never by hand; scaffold with `dg scaffold defs`; use its references for assets, schedules, sensors, automation and resources. Check every Dagster
   name and signature with it, `dg` (`dg list components --json`, `--help`) or the installed source. Do not invent an API.
2. Load the **`dagster-community-components`** skill (by name; for agents that load skills from files: `{DCC}` in this repository, with its `reference/cli.md`, `reference/catalog.md`,
   `reference/writing-yaml.md` and `reference/demo-workflow.md`). Follow its hard rules: never invent a component id, config field or CLI flag; search by capability and vendor before concluding that nothing exists;
   read the real `schema.json` fields before writing any `defs.yaml`; registry component first, official `dagster-<vendor>` integration second, hand-written code last.
3. Follow `reference/demo-workflow.md` of that skill for every stage of this DAG: **search** (`dagster-component search "<stage keywords>" --json`), **verify** that stages chain (`produces` / `consumes`; the component's README
   `Pairs with`), **read the schema** (`dagster-component info <id>`, `dagster-component schema <id> --format json`), **add and wire** (`dagster-component add <id>`, a `defs.yaml` using only fields you read), **validate**
   (`dg check defs`, `dg list defs --json`). `dagster-component list --available` lists the registry (plain `list` shows installed components). `dg list components --json` lists what is installed in the project.
4. For each registry component you choose, say in the header docstring which id you used, which schema fields you set, and its validation tier (`validation.level` in `dagster-component search --json`: `live`, `code`, `unit`,
   `component_only`, or none). A component that is not `live` carries a caveat sentence in the docstring. If you use an official integration or hand-written code instead, say why no registry component fit.

## 2. The specification (facts; statements marked inferred are not facts)

{spec_md}

## 3. What to build: the target idioms

**The principle: native Dagster forms first; plain Python inside the asset is the stated fallback.** For branching, short circuits, skips, rejoins and runtime fan-out Dagster has native forms, verified by running
on the installed version and shown in the patterns below when the spec shows the construct: a ShortCircuit is an optional-output asset or op; a branch to different downstream assets/ops is a `@multi_asset` with
`AssetOut(is_required=False)` or an op with `Out(is_required=False)` (steps below an unyielded output are skipped, the run succeeds); a branch then rejoin to one product is a `@graph_asset` whose join op takes a
fan-in `list`; a runtime list is `DynamicOut` + `.map()`. Use them. Plain Python inside one asset (`if/else`, early `return`, `for`, `try/finally`, local variables for XCom) is the FALLBACK, for only three cases:
(a) the branches produce the same output and differ only in logic, (b) the native form is not verified to work for this case, (c) it failed a gate; the decision record then says `fallback to plain Python:
<reason>`. The product-granularity principle stays (one asset per data product, not one per task: tasks that only prepare a single output fold into its asset) but it is not a reason to fold a real branch into plain
Python. Dagster fixes a run's shape before it starts, so a runtime decision is an optional output or code inside an asset, never a different set of assets chosen at run time (`can_subset` is launch-time selection;
do not add it; verified unnecessary). Folding into plain Python loses the skipped state, per-step retries, per-step visibility and partial restart: say so in the decision record.

- **One asset per data product**, not per task. A data product is a task whose output is persisted (a table, file, S3 key, dbt model) or a returned value consumed by more than the next step. Tasks that only prepare or
  help produce one output are plain Python inside that asset.
- **Ops only where a restart point matters.** Use a graph-backed asset (`@dg.graph_asset` / `@dg.graph_multi_asset`; check the real API with `dagster-expert`) with ops when a step has retries, a long runtime or an expensive
  external call and deserves its own retry; otherwise keep it plain Python and say that a failed step restarts from the start of the asset.
- **A chain of dataframe or SQL transformations is ONE pipeline component asset** (`polars_pipeline`, `pandas_pipeline`, `warehouse_pipeline`, `pyspark_pipeline` in the registry) when the steps map to the component's
  ops: read its `schema.json` first and use only those ops. If the spec's code does something the ops cannot express, keep that step as Python and say so.
- **Notification, wait and cleanup tasks are not assets.** Email, Slack and other notifications become a run-status sensor, a hook or part of the producing asset; sensors-as-waits become Dagster sensors or an automation
  condition; `EmptyOperator` disappears. The header must say which for each.
- Dagster's own model for the rest: schedules with the spec's cron and timezone (`dg.ScheduleDefinition`), retry policies from the spec, pools as the first-class `pool=` on the asset or op (the limit is instance configuration: `dagster instance concurrency set <pool> <n>`; say who sets it), `AutomationCondition` for asset-triggered DAGs, dbt through `DbtProjectComponent`
  for Cosmos, connections as resources or environment variables **by name** (never a value), Variables as environment variables.
- A task that needs its own Python environment (`PythonVirtualenvOperator`, `ExternalPythonOperator`, `@task.virtualenv`): declare the requirements and run it through Dagster Pipes (`PipesSubprocessClient` with that environment's interpreter), with a loud failure when the interpreter is missing; never silently run it in the current interpreter (the venv pattern is injected when the spec shows one).
- Add **non-blocking structural asset checks** only where the source shows something checkable (a consumed value is not None, an S3 object written exists). They are structural, not business, checks: do not invent business rules.
- Preserve every behaviour under "must be preserved" in the spec. That includes the skip and branch decisions and what each task's end state was in Airflow (`success`, `failed`, `skipped`, `upstream_failed`): a task's
  `skipped` state is kept (its own asset with `airflow/state` metadata, or a skippable output: see the patterns) or its loss is declared in the decision record; it is never silently dropped. A branch task that is not taken and is folded into the branching asset is accounted for as `folded into `<asset>`: branch not taken` and declared under `skip/branch`. Of the "acceptable differences", use only
  those that name something Dagster cannot express.

{patterns}## 4. Where to write it

- The module `{package}.defs.{d}` (`src/{package}/defs/{d}.py`), plus any component folders under `src/{package}/defs/`. Define a job named `{d}` over the DAG's assets and the schedule from the spec.
- The first lines of the module are a docstring that **accounts for every Airflow task**: one line per task id in the form `<task_id>: replaced by asset `<asset key>`` / `folded into `<asset key>`` /
  `dropped: <reason>` / `merged into ...`, followed by the registry components and tiers you used and any behaviour you could not preserve (stated, not hidden).
- Under the accounting, a **decision record**: a paragraph that starts with the line `Decision record:` and then one line per non-obvious behaviour in the form `- <behaviour>: preserved` or
  `- <behaviour>: not preserved: <why>`. Behaviours: `skip/branch`, `trigger rules`, `retries`, `pools`, `sensor modes`, `depends_on_past`, `failure semantics`, `mapped tasks`. Write a line for every behaviour the spec
  shows (the gate lists which it expects); `not preserved` needs a reason, and where folding loses per-step retries or partial restart, name the alternative (`RetryPolicy`, a graph asset). Declaring a loss is accepted;
  hiding it is not. In a many-tasks-into-one-asset rewrite the accounting is still one line per task: `<task_id>: folded into `<asset>``.
- Secrets: environment variables and resources only. No password, token, key or URL with credentials in the code or YAML.

## 5. The acceptance gates

Your rewrite is accepted only when `python3 scripts/accept_rewrite.py --spec rewrite/{d}.spec.json --module {package}.defs.{d} --project <project root> [--defs-dir <component folder>] [--parity <Airflow results>] --manifest manifest.json`
passes every gate (nine):

1. `dg check defs` passes (or, where `dg` is not installed, the definitions load; the gate says which);
2. the generated pytest passes: the module loads, a job named `{d}` exists, the schedule's cron and timezone equal Airflow's, and for a DAG without external systems the DAG materializes and every asset check passes; when the Airflow baseline shows the DAG's own run failing (by design, or on a virtualenv / external-interpreter task that cannot build), the test asserts that the rewrite fails too, in a step, instead of succeeding;
3. asset checks are present where the source supports them (at least {len((cv.get('asset_checks') or []))} here);
4. parity: where an Airflow baseline exists, the same overall outcome and the same value as Airflow for every task the header says is `replaced by` an asset (a task `folded into` another has no value of its own to compare);
5. no secret values;
6. no invented APIs: every import and every component `type:` resolves;
7. every Airflow task is accounted for in the header docstring;

8. **task states**: where an Airflow baseline is available (`tools/baseline/`, see `TASK.md`), run the project and compare each task's final state (success, failed, skipped, upstream_failed) and the observable effects
   with Airflow's; the gate names the task that differs. A task folded into another asset with Airflow state `skipped` (a branch not taken) is an accepted representation difference ONLY when the decision record declares it;
9. **decision record**: the header has the `Decision record:` section and it covers every behaviour the spec flags.

A rewrite that fails a gate stays deferred, with the failure recorded; fix it and run the gates again. Never mark a unit complete yourself.

## 6. The converter's output (reference and fallback)

{ref}

If the rewrite cannot pass the gates, the converter's output is what ships (or the DAG stays deferred where the converter deferred it). Say plainly in your final message what you could not preserve or verify.

## 7. Cost

This route spends model effort on every DAG (reading the spec, searching the registry, writing and iterating until the gates pass). Use it for the DAGs where idiomatic output or wider operator coverage matters; the
converter stays the default for mechanical DAGs.
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", type=Path, help="a <dag>.spec.json, or a folder of them")
    ap.add_argument("--converter-module", help="path to the converter's generated module for this DAG (reference draft)")
    ap.add_argument("--project", default="my_project")
    ap.add_argument("--package", default="my_pkg")
    ap.add_argument("--out-dir", type=Path)
    a = ap.parse_args(argv)
    specs = sorted(a.spec.glob("*.spec.json")) if a.spec.is_dir() else [a.spec]
    for p in specs:
        spec = json.loads(p.read_text())
        md = p.with_name(p.name.replace(".spec.json", ".spec.md")).read_text()
        out = (a.out_dir or p.parent) / f"{spec['dag_id']}.prompt.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render(spec, md, a.converter_module, a.project, a.package))
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
