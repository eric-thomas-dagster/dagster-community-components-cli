#!/usr/bin/env python3
"""Write `rewrite/<dag_id>.prompt.md`: a self-contained prompt that asks Claude to write one DAG's Dagster code from scratch, from the spec `dag_spec.py` wrote.

    python3 rewrite_prompt.py rewrite/<dag>.spec.json [--converter-module path/to/defs/<dag>.py] [--converter-project DIR] [--spec-ref spec.md] [--project <name>] [--package <pkg>] [--out-dir rewrite]
    python3 rewrite_prompt.py rewrite/ ...                       (every *.spec.json in the folder)

This is the second route inside tier 3 (Dagster owns the code): the converter (`convert_ir.py`) transforms the source almost like for like; the rewrite route builds a thorough understanding of
what the DAG does and has Claude, with the `dagster-expert` and `dagster-community-components` skills and the component registry, write idiomatic Dagster from that. The converter's output is the
reference draft and the fallback. The prompt is deterministic text around the spec; it contains no secret value (the spec has none). It spends model effort per DAG: use it where it earns it
(`SKILL.md`, "When to use the rewrite route").

Options (all default off; the default prompt is the same cold-worker prompt for every DAG, sized by what the DAG's spec shows):
  --converter-project DIR   the converter's emitted project (`DIR/report.json`, `DIR/src/<pkg>/defs/<dag>.py`); the prompt lists its stubs, degraded sections and notes
  --converter-mode MODE     reference (default): rewrite from the original and the spec; the project is read-only reference (copy verified logic, not structure; the stub list is the hard parts).
                            start: improve the converter's project (its list is the TODO list)
  --converter-module PATH   the older form: only name the converter's module as a reference draft
  --spec-ref NAME           do not paste the specification into the prompt; point at the file NAME in the worker's folder (a kit already has `spec.md`)

Sections that depend on the spec: the service-operator resource table (`rewrite_resources.py`, only the families the DAG uses), the registry-search checklist (one line per stage), the patterns (`rewrite_patterns.py`) and the
long native-forms paragraph (only when the spec shows a branch, short circuit, skip or mapping).
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import rewrite_patterns
import rewrite_resources

DCC = "skills/dagster-community-components/SKILL.md"


def safe_id(dag_id: str) -> str:
    return re.sub(r"\W", "_", dag_id)


def converter_start(spec: dict, directory: Path) -> dict | None:
    """What the converter emitted for this DAG, read from `DIR/report.json` (or `conversion_report.json`): its module, stubs, degraded sections and notes. None when the report has no entry for the DAG."""
    directory = Path(directory)
    rp = next((p for p in (directory / "report.json", directory / "conversion_report.json") if p.exists()), None)
    if rp is None:
        raise FileNotFoundError(f"no report.json in the converter project {directory}")
    e = ((json.loads(rp.read_text()).get("dags")) or {}).get(spec["dag_id"])
    if not e:
        return None
    mods = sorted(directory.glob(f"src/*/defs/{safe_id(spec['dag_id'])}.py")) or sorted(directory.glob(f"**/defs/{safe_id(spec['dag_id'])}.py"))
    cav = e.get("caveats") or []
    return {"dir": str(directory), "module": str(mods[0].relative_to(directory)) if mods else (e.get("output") or None), "status": e.get("status"), "reason": e.get("reason"),
            "stubs": [c for c in cav if c.get("kind") == "stub"], "degraded": [c for c in cav if c.get("kind") != "stub"], "notes": list(e.get("notes") or []), "shims": list(e.get("shims") or []),
            "local_modules": list(e.get("local_modules") or []), "requirements": list(e.get("requirements") or []), "runtime": bool(list(directory.glob("src/*/runtime.py")))}


CONVERTER_MODES = ("reference", "start")


def converter_reference_section(cp: dict, task_ids: list[str] | None = None) -> str:
    """Reference mode: the original DAG and the spec are the source of truth; the converter's project is read-only reference material."""
    flagged = {c.get("task") for c in cp["stubs"] + cp["degraded"] if c.get("task")}
    clean = [t for t in (task_ids or []) if t not in flagged]
    L = [f"The converter's emitted project is at `{cp['dir']}`; its module for this DAG is `{cp['module']}` (status `{cp['status']}`" + (f", reason: {cp['reason']}" if cp.get("reason") else "") + ").",
         "**Use it as a read-only reference, not as a starting point.** Rewrite from the original DAG and the specification. Do not copy the converter's project into yours and do not inherit its structure: no shims everywhere, no task-per-asset, "
         "no gate runtime where a native form fits. What you may take from it is logic it verified: an operator body, a SQL string, an argument mapping, a shim that reproduces a real operator's behaviour. Copy that logic into the asset it belongs to and keep the structure from section 3. "
         "If the converter and the spec disagree, the spec (the original DAG) wins.", "",
         "The converter's stub list is the list of hard parts: the places it could not translate. Do the real work there (each item is fixed, or declared `not preserved: <why>` in the decision record):"]
    L += [f"- STUB `{c.get('task') or 'DAG'}`: {c.get('reason')}" for c in cp["stubs"]]
    L += [f"- DEGRADED `{c.get('task') or 'DAG'}`: {c.get('reason')} (a known difference from Airflow: remove it or declare it)" for c in cp["degraded"]]
    L += [f"- note: {n}" for n in cp["notes"]]
    if not (cp["stubs"] or cp["degraded"] or cp["notes"]):
        L.append("- none: the report lists no stub, degraded section or note (that is not proof it is right: verify below)")
    if clean:
        L.append(f"- tasks the converter converted without a stub or caveat: {', '.join(clean)} (their logic is the candidate for copying; the behaviour comparison, when you have it, says whether they match Airflow)")
    if cp["shims"]:
        L.append(f"- shims it uses (verified against the real operator by the converter's tests; use one only where no real operator, library call or registry resource fits, and say why): {', '.join(str(x) for x in cp['shims'])}")
    if cp["local_modules"]:
        L.append(f"- the DAG imports project modules {cp['local_modules']}: copy them into the project (they are not pip packages)")
    if cp["requirements"]:
        L.append(f"- packages it needs: {', '.join(str(r) for r in cp['requirements'])}")
    L += ["", "The same gates apply as for a rewrite from the spec alone, and the same checks: the header accounts for every task with the asset key that exists; the decision record covers every behaviour the spec flags; task end states "
          "(`skipped` kept or declared); service calls use the registry resource where one exists (table in section 3) and say so in the registry-search lines; no secret value; then run the gates and the behaviour comparison.",
          "In the decision record, say what you took from the converter (which logic, which shims) and what you did not take and why."]
    return "\n".join(L)


def converter_section(cp: dict) -> str:
    L = [f"The converter's emitted project is at `{cp['dir']}`; its module for this DAG is `{cp['module']}` (status `{cp['status']}`" + (f", reason: {cp['reason']}" if cp.get("reason") else "") + ").",
         "**Start from it**: copy that module (and `runtime.py` beside it if the module imports it) into your project, make it pass `dg check defs`, then improve it. This is a starting point, not a requirement: where its structure is wrong for the spec "
         "(one asset per task, a gate runtime where a native optional output fits, a service call written by hand where a registry resource exists) replace it. Its list below is your TODO list.", "",
         "TODO from the converter's report (each item is fixed, or declared in the decision record):"]
    L += [f"- STUB `{c.get('task') or 'DAG'}`: {c.get('reason')} (a loud `AIRFLOW-CAVEAT` stub: write the real behaviour or declare `not preserved: <why>`)" for c in cp["stubs"]]
    L += [f"- DEGRADED `{c.get('task') or 'DAG'}`: {c.get('reason')} (a known difference from Airflow: remove it or declare it)" for c in cp["degraded"]]
    L += [f"- note: {n}" for n in cp["notes"]]
    if cp["shims"]:
        L.append(f"- shims it uses (check each against the real operator): {', '.join(str(x) for x in cp['shims'])}")
    if cp["local_modules"]:
        L.append(f"- the DAG imports project modules {cp['local_modules']}: copy them into the project (they are not pip packages)")
    if cp["requirements"]:
        L.append(f"- packages it needs: {', '.join(str(r) for r in cp['requirements'])}")
    if not (cp["stubs"] or cp["degraded"] or cp["notes"]):
        L.append("- none: the report lists no stub, degraded section or note (that is not proof it is right: verify below)")
    L += ["", "Verify what the converter does NOT do for you, whatever it reports: the header accounts for every task with the asset key that exists; the decision record covers every behaviour the spec flags; task end states "
          "(`skipped` kept or declared); service calls use the registry resource where one exists (table in section 3) and say so in the registry-search lines; no secret value; then run the gates and the behaviour comparison."]
    return "\n".join(L)


SPEC_STUB = re.compile(r"## What the converter did \(reference draft, not a requirement\)\n\n- no conversion report was given\n\n(?=## Parity baseline)")


def trim_spec(spec_md: str, drop_converter: bool = False) -> str:
    """The spec without its empty 'what the converter did' stub (and, when the prompt lists the converter's TODO itself, without that section): the same facts, not repeated."""
    md = SPEC_STUB.sub("", spec_md)
    if drop_converter:
        md = re.sub(r"## What the converter did \(reference draft, not a requirement\)\n.*?(?=## Parity baseline)", "", md, flags=re.S)
    return md


PRINCIPLE_FULL = """**The principle: native Dagster forms first; plain Python inside the asset is the stated fallback.** For branching, short circuits, skips, rejoins and runtime fan-out Dagster has native forms, verified by running
on the installed version and shown in the patterns below: a ShortCircuit is an optional-output asset or op; a branch to different downstream assets/ops is a `@multi_asset` with `AssetOut(is_required=False)` or an op with
`Out(is_required=False)` (steps below an unyielded output are skipped, the run succeeds); a branch then rejoin to one product is a `@graph_asset` whose join op takes a fan-in `list`; a runtime list is `DynamicOut` + `.map()`.
Use them. Plain Python inside one asset (`if/else`, early `return`, `for`, `try/finally`, local variables for XCom) is the FALLBACK, for only three cases: (a) the branches produce the same output and differ only in logic,
(b) the native form is not verified to work for this case, (c) it failed a gate; the decision record then says `fallback to plain Python: <reason>`. One asset per data product still holds but is not a reason to fold a real
branch into plain Python. Dagster fixes a run's shape before it starts, so a runtime decision is an optional output or code inside an asset, never a different set of assets chosen at run time (`can_subset` is launch-time
selection; do not add it; verified unnecessary). Folding into plain Python loses the skipped state, per-step retries, per-step visibility and partial restart: say so in the decision record."""
PRINCIPLE_SHORT = """**The principle: native Dagster forms first; plain Python inside the asset is the stated fallback** (then `fallback to plain Python: <reason>` in the decision record). An asset is a data product; the tasks inside it are
ops or plain Python. Dagster fixes a run's shape before it starts: a runtime decision is an optional output or code inside an asset, never a different set of assets chosen at run time."""


def render(spec: dict, spec_md: str, converter_module: str | None, project: str, package: str, converter_project: dict | None = None, spec_ref: str | None = None, converter_mode: str = "reference") -> str:
    """`converter_mode` applies only when `converter_project` is given: `reference` (default: the original DAG is the source, the converter's project is read-only reference) or `start` (improve the converter's project)."""
    if converter_mode not in CONVERTER_MODES:
        raise ValueError(f"converter_mode must be one of {CONVERTER_MODES}, not {converter_mode!r}")
    start_mode = converter_mode == "start"
    d = spec["dag_id"]
    cv = spec.get("converter") or {}
    pat_names = rewrite_patterns.detect(spec)
    pats = rewrite_patterns.render(pat_names)
    patterns = (pats + "\n") if pats else ""
    fams = rewrite_resources.families(spec)
    resources = (rewrite_resources.RESOURCE_RULE.format(version=rewrite_resources.REGISTRY_VERSION) + ("\n\n" + rewrite_resources.resource_table(fams) if fams else "")) + "\n"
    checklist = rewrite_resources.checklist_text(spec, pat_names)
    full = bool(set(pat_names) & rewrite_patterns.SKIP_FAMILY)
    if converter_project and start_mode:
        ref = converter_section(converter_project)
        intro_conv = "A converter's project is the starting point (section 6): improve it, do not transcribe the Airflow source."
    elif converter_project:
        ref = converter_reference_section(converter_project, [t.get("task_id") for t in spec.get("tasks", []) if t.get("task_id")])
        intro_conv = "A converter's project is given as read-only reference (section 6); the original DAG and this specification are the source of truth: do not inherit the converter's structure."
    elif converter_module:
        ref = (f"The converter's output for this DAG is at `{converter_module}` (status `{cv.get('status')}`; stubbed tasks: {', '.join(cv.get('stubbed') or []) or 'none'}). Read it as a draft: it shows the "
               "source's behaviour translated one task at a time. Do not copy its structure; use it to check that you have not lost behaviour.\n\nIf the rewrite cannot pass the gates, the converter's output is what ships.")
        intro_conv = "The converter's output is a draft and the fallback if your rewrite fails the gates; it is not the model to imitate."
    else:
        ref = (f"No converter output is given (converter status: `{cv.get('status')}`" + (f", reason: {cv.get('reason')}" if cv.get("reason") else "") + "). Work from the spec alone. If the rewrite cannot pass the gates, the converter's output ships instead.")
        intro_conv = "A converter's output exists as the fallback if your rewrite fails the gates; it is not the model to imitate."
    spec_text = (f"The specification is the file `{spec_ref}` next to this prompt (the same facts as `spec.json`): read it before anything else; do not ask for it again here. Statements in it marked inferred are not facts." if spec_ref
                 else trim_spec(spec_md, drop_converter=bool(converter_project)))
    bullets = ["- **One asset per data product**, not per task. A data product is a task whose output is persisted (a table, file, S3 key, dbt model) or a returned value consumed by more than the next step. Tasks that only prepare or "
               "help produce one output are plain Python (or ops) inside that asset.",
               "- **Ops only where a restart point matters.** Use a graph-backed asset (`@dg.graph_asset` / `@dg.graph_multi_asset`; check the real API with `dagster-expert`) with ops when a step has retries, a long runtime or an expensive "
               "external call and deserves its own retry; otherwise plain Python, and say that a failed step restarts from the start of the asset.",
               "- **A chain of dataframe or SQL transformations is ONE pipeline component asset** (`polars_pipeline`, `pandas_pipeline`, `warehouse_pipeline`, `pyspark_pipeline`) when the steps map to the component's ops: read its "
               "`schema.json` first and use only those ops; a step the ops cannot express stays Python, said so.",
               "- **Notification, wait and cleanup tasks are not assets.** Email and Slack are resource calls (above) inside the producing asset or a hook, or a run-status sensor; sensors-as-waits are Dagster sensors or an automation condition; "
               "`EmptyOperator` disappears. The header says which for each.",
               "- Schedules with the spec's cron and timezone (`dg.ScheduleDefinition`), retry policies from the spec" + (", pools as the first-class `pool=` on the asset or op (the limit is instance configuration: `dagster instance concurrency set <pool> <n>`; say who sets it)" if "pools" in pat_names else "")
               + (", `AutomationCondition` for asset-triggered DAGs" if "cross_dag" in pat_names else "") + (", dbt through `DbtProjectComponent` for Cosmos" if spec.get("cosmos_groups") else "")
               + "; connections as resources or environment variables **by name** (never a value), Variables as environment variables."]
    if "venv_pipes" in pat_names:
        bullets.append("- A task that needs its own Python environment (`PythonVirtualenvOperator`, `ExternalPythonOperator`, `@task.virtualenv`): declare the requirements and run it through Dagster Pipes (`PipesSubprocessClient` with that environment's "
                       "interpreter), with a loud failure when the interpreter is missing; never silently run it in the current interpreter (the pattern below).")
    bullets += ["- Add **non-blocking structural asset checks** only where the source shows something checkable (a consumed value is not None, an S3 object written exists); not business checks.",
                "- Preserve every behaviour under \"must be preserved\" in the spec, including each task's Airflow end state (`success`, `failed`, `skipped`, `upstream_failed`): a `skipped` state is kept (its own asset with `airflow/state` metadata, "
                "or a skippable output: see the patterns) or its loss is declared in the decision record; it is never silently dropped. A branch task not taken and folded into the branching asset is `folded into `<asset>`: branch not taken` "
                "and declared under `skip/branch`. Of the \"acceptable differences\", use only those that name something Dagster cannot express."]
    bullets_text = "\n".join(bullets)
    return f"""# Rewrite Airflow DAG `{d}` as idiomatic Dagster

You are writing the Dagster code for one Airflow DAG. Do not transform the Airflow source line by line: understand what the DAG does from the specification below, then build what a Dagster engineer would build for that behaviour. {intro_conv} This route spends model effort per DAG.

## 1. Skills and the registry

1. Load **`dagster-expert`** (by name) and follow its component hierarchy: components and registry/official integrations before hand-written code; create the project with `uvx create-dagster project {project} --uv-sync` if it does not exist,
   never by hand; scaffold with `dg scaffold defs`. Check every Dagster name and signature with it, `dg` (`dg list components --json`, `--help`) or the installed source. Do not invent an API.
2. Load **`dagster-community-components`** (by name; as a file: `{DCC}`, with its `reference/cli.md`, `reference/catalog.md`, `reference/writing-yaml.md`, `reference/demo-workflow.md`). Hard rules: never invent a component id, config field or CLI flag; read the real
   `schema.json` before writing a `defs.yaml`; registry component or resource first, official `dagster-<vendor>` integration second, hand-written code last.
3. **Check the registry for every stage before writing it, and record it** (the search lines in section 4 are the checklist; an unsearched stage is incomplete work): `dagster-component search "<stage keywords>" --json` (`list --available` lists the registry), a hit: `dagster-component info <id>` and
   `dagster-component schema <id> --format json`, then `dagster-component add <id>`, a `defs.yaml` using only fields you read, and `dg check defs` / `dg list defs --json`. Stages chain through `produces` / `consumes` (README `Pairs with`).
4. For each registry component or resource you use, the header says which id, which schema fields you set and its validation tier (`validation.level`: `live`, `code`, `unit`, `component_only`, or none); a component that is not `live` carries a caveat sentence. If you use an official integration or hand-written code, say why no registry component fit.

## 2. The specification (facts; statements marked inferred are not facts)

{spec_text}

## 3. What to build: the target idioms

{PRINCIPLE_FULL if full else PRINCIPLE_SHORT}

{bullets_text}

{resources}
{patterns}## 4. Where to write it

- Under `src/{package}/defs/`, in either shape (the gates accept both on equal terms; they load the project through its own definitions loader and find assets, the job and the schedule by KEY and NAME):
  - **module style**: `{package}.defs.{d}` (`src/{package}/defs/{d}.py`) with `@dg.asset` functions; or
  - **component style**: one folder per registry component under `src/{package}/defs/<id>/` with a `defs.yaml` (from `dagster-component add <id>`, filled from the real `schema.json`), plus a small `.py` next to it for the job and the schedule
    (a module-level `dg.define_asset_job("{d}", selection=dg.AssetSelection.assets("<key>"))` and `dg.ScheduleDefinition(...)`).
  A project may mix both; **component projects are preferred whenever a registry component fits a stage**. Define a job named `{d}` over the DAG's assets and the schedule from the spec.
- The header **accounts for every Airflow task**: one line per task id: `<task_id>: replaced by asset `<asset key>`` / `folded into `<asset key>`` / `dropped: <reason>` / `merged into ...` (the asset key must exist in the loaded definitions), then the registry
  components and tiers used and any behaviour you could not preserve (stated, not hidden). The header is the docstring at the top of a module under `defs/` OR the `#` comment block at the top of a component's `defs.yaml`; it may be split across files.
- Under the accounting, a **decision record**: a paragraph starting with the line `Decision record:`, then one line per non-obvious behaviour: `- <behaviour>: preserved` or `- <behaviour>: not preserved: <why>` (or `fallback to plain Python: <why>`).
  Behaviours: `skip/branch`, `trigger rules`, `retries`, `pools`, `sensor modes`, `depends_on_past`, `failure semantics`, `mapped tasks`: a line for every one the spec shows (the gate lists which it expects). Where folding loses per-step retries or
  partial restart, name the alternative (`RetryPolicy`, a graph asset). Declaring a loss is accepted; hiding it is not. In a many-tasks-into-one-asset rewrite the accounting is still one line per task.
- Secrets: environment variables and resources only. No password, token, key or URL with credentials in the code or YAML.

{checklist}

## 5. The acceptance gates

Accepted only when `python3 scripts/accept_rewrite.py --spec rewrite/{d}.spec.json --project <project root> [--parity <Airflow results>] --manifest manifest.json` (in a kit: the command in `TASK.md`) passes all nine gates: (1) `dg check defs`; (2) the generated pytest: the definitions load
through the project's loader, a job named `{d}` exists, the schedule's cron and timezone equal Airflow's, and a DAG without external systems materializes and every asset check passes (when the Airflow baseline shows the DAG's own run failing, the rewrite must fail too, in a step); (3) asset checks present
where the source supports them (at least {len((cv.get('asset_checks') or []))} here); (4) parity: the same outcome and value as Airflow for every task the header says is `replaced by` an asset (a `folded into` task has no value of its own); (5) no secret values; (6) no invented APIs: every import and
component `type:` resolves; (7) every Airflow task is accounted for in the header and its asset exists in the loaded definitions; (8) **task states**: each task's final state (success, failed, skipped, upstream_failed) and observable effects against Airflow's where a baseline is available (a folded task with
state `skipped`, a branch not taken, is accepted ONLY when the decision record declares it); (9) **decision record**: the `Decision record:` section covers every behaviour the spec flags. A failed gate stays deferred with the failure recorded; fix it and run again. Never mark a unit complete yourself.

## 6. {("The converter's project (starting point)" if start_mode else "The converter's project (read-only reference)") if converter_project else "The converter's output"}

{ref}

Say plainly in your final message what you could not preserve or verify.
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", type=Path, help="a <dag>.spec.json, or a folder of them")
    ap.add_argument("--converter-module", help="path to the converter's generated module for this DAG (reference draft)")
    ap.add_argument("--converter-project", type=Path, help="the converter's emitted project (report.json + src/<pkg>/defs/<dag>.py): its stubs, degraded sections and notes are the list of hard parts (see --converter-mode). Default off")
    ap.add_argument("--converter-mode", choices=CONVERTER_MODES, default="reference", help="with --converter-project: `reference` (default; the original DAG and the spec are the source, the converter's project is read-only reference: copy logic it verified, not its structure) or `start` (improve the converter's project)")
    ap.add_argument("--spec-ref", help="do not paste the specification: point at this file in the worker's folder (for example spec.md)")
    ap.add_argument("--project", default="my_project")
    ap.add_argument("--package", default="my_pkg")
    ap.add_argument("--out-dir", type=Path)
    a = ap.parse_args(argv)
    specs = sorted(a.spec.glob("*.spec.json")) if a.spec.is_dir() else [a.spec]
    for p in specs:
        spec = json.loads(p.read_text())
        md = p.with_name(p.name.replace(".spec.json", ".spec.md")).read_text()
        cp = converter_start(spec, a.converter_project) if a.converter_project else None
        out = (a.out_dir or p.parent) / f"{spec['dag_id']}.prompt.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render(spec, md, a.converter_module, a.project, a.package, converter_project=cp, spec_ref=a.spec_ref, converter_mode=a.converter_mode))
        print(f"wrote {out}" + (" (no converter entry for this DAG: spec alone)" if a.converter_project and not cp else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
