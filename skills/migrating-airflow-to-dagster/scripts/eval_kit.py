#!/usr/bin/env python3
"""Generate the cold-worker kit: for each sampled DAG, a folder with exactly what a fresh agent needs to write the rewrite, and nothing else.

    python3 eval_kit.py make  --work DIR --manifest corpus/manifest.json --sample sample.json --run stage1 [--dcc-skill PATH] [--baseline-from RUN | --venv-2 PY --venv-3 PY]
    python3 eval_kit.py check --work DIR --run stage1 [--manifest corpus/manifest.json --sample sample.json]

`make` writes, per DAG, DIR/<run>/<dag>/ :
    TASK.md             what to produce and where, the gates, the sandbox contract, the rules
    WORKER_REPORT.md    the template the worker fills in (route, files, registry components, assumptions, gaps, attempts)
    spec.md, spec.json  the facts-only spec (`dag_spec.py`), generated WITHOUT any converter data and WITHOUT an Airflow baseline
    prompt.md           the generated rewrite prompt (`rewrite_prompt.py`), unchanged except the module name when the DAG id is not a Python identifier
    source/             the original DAG file and its local dependencies (modules it imports, data files it names), at their repository paths
    tools/              the acceptance gates (accept_rewrite.py and the modules it imports), a sandbox runner, and `compare_behavior.py` with `tools/baseline/` (the ORIGINAL DAG's results from real Airflow,
                        run twice when the kit is built, this DAG only), so the worker can check its own work and see where its behaviour differs from Airflow's without Airflow installed
    out/                empty: the worker creates its Dagster project here
and DIR/<run>-private/ (kit index, private specs): never given to workers.

Baselines: `--baseline-from RUN` copies the raw Airflow results (`airflow1.json`, `airflow2.json`) that an earlier run of the same DAGs already produced (`<work>/<RUN>-private/behavior/<dag>/`; same DAG, same
Airflow, nothing re-run); `--venv-2/--venv-3` run the original DAG twice now. With neither, the kit has no baseline and `tools/compare_behavior.py` says so. Paths are scrubbed.

ARM C (a sample entry with `"arm": "C"`, with `--converter-dir`): arm A plus `reference/converter/` (the converter's project for THIS DAG, read-only, report reduced to the DAG's entry) and the prompt in `--converter-mode reference`;
TASK.md rules 2 and 6 are relaxed for this arm only and say so. Cold arms (A, B) are unchanged.

`check` fails when a worker folder holds converter output (a converted module, the shared runtime, a conversion report, a caveat marker) or any other sampled DAG's data (its id, its source file,
its spec), or a path of this machine's user. It also verifies the kit is self-contained: every module a tool imports is in `tools/`, the baseline is present and holds nothing but this DAG's Airflow run. It is the guard that the rewrites are cold.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import eval_common as C
import rewrite_prompt

HERE = Path(__file__).resolve().parent
CONVERTER_MARKERS = ("AIRFLOW-CAVEAT", "Converted from Airflow DAG", "af_caveat(", "from ..runtime import", "convert_ir.py", "converted_requirements")
FORBIDDEN_FILES = ("runtime.py", "report.json", "conversion_report.json", "converted_requirements.txt")

TASK_MD = """# Task: rewrite Airflow DAG `{dag_id}` as Dagster code

You are one of several independent workers. You write the Dagster code for ONE DAG, from the specification in this folder, and nothing else. Your result is measured by running it against the original
DAG in real Airflow, so what it DOES matters more than how it looks.

## What is in this folder

| Path | What |
|---|---|
| `spec.md` / `spec.json` | the facts about the DAG: tasks, operator arguments, callables' source, dependencies, value flow, schedule, retries, pools, connection and Variable names |
| `prompt.md` | the full instructions for the rewrite (skills to load, target idioms, header accounting, gates). Follow it; where it conflicts with this file, this file wins |
| `source/` | the original DAG file and the local files it imports or reads, at their repository paths. Read the source: the spec is a summary of it |
| `tools/` | `accept_rewrite.py` (the acceptance gates), `run_in_sandbox.py` (runs your DAG in the sandbox the measurement uses){tools_extra} |
{ref_row}| `out/` | where your Dagster project goes |
| `WORKER_REPORT.md` | fill it in when you finish |

## What to produce

1. A Dagster project under `out/{project}/`, created with `cd out && uvx create-dagster project {project} --uv-sync` (never by hand), so it has its own `.venv` with Dagster. Add what your code needs with
   `uv add` (for example `pytest`, `requests`, `moto[s3]`) inside the project.
2. The rewrite under `out/{project}/src/{project}/defs/`, in either shape (both are accepted on equal terms; the gates load your project through its own definitions loader and find assets, the job and the
   schedule by key and name, never by variable name): **module style**, `defs/{module}.py` with `@dg.asset` functions; or **component style**, a folder per registry component under `defs/<id>/` with a `defs.yaml`
   (`dagster-component add <id>` creates it), plus a small `.py` next to it for the job and the schedule. **When a registry component fits a stage, a component project is preferred** (registry first). The header
   that accounts for every Airflow task (`prompt.md`, section 4: `<task_id>: replaced by asset `<key>`` / `folded into `<key>`` / `merged into `<key>`` / `dropped: <reason>`) is the docstring of any module under
   `defs/`, or the `#` comment block at the top of a `defs.yaml`. A job named `{dag_id}` over the DAG's assets, and the schedule from the spec.
3. Your own tests under `out/{project}/tests/` that check what the DAG does (values, files, rows, requests it makes), not only that it loads.
4. `WORKER_REPORT.md` filled in.

## The acceptance gates (you run them; they are necessary, not sufficient)

```
cd out/{project} && .venv/bin/python ../../tools/accept_rewrite.py --spec ../../spec.json --project .{gate_baseline}
```
(the project, its modules and its component folders are discovered under `src/{project}/defs/`; there is nothing to name)

Nine gates: `dg check defs`, a generated behavioural test, asset checks, parity (not run here), no secret values, no invented APIs, every task accounted for, **task states**{states_note} and **decision record** (see rule 8).
Passing the gates does not mean the behaviour is right.

{feedback}
## The sandbox your code runs in (the same for the original DAG)

- The working directory is a scratch folder that holds the project files shown in `source/` at their repository paths (for example `include/data.csv`), unchanged. Write files relative to it (the spec's paths are what the original wrote); do not use absolute paths outside it.
- Connections are environment variables `AIRFLOW_CONN_<CONN_ID>` holding a connection URI (what Airflow itself reads): read the host, login, schema from there instead of hardcoding. Defaults exist
  for `sqlite_default`, `http_default`, `smtp_default`, `aws_default`. Variables are `AIRFLOW_VAR_<NAME>` (upper case). Never put a credential in code.
- There is no network. HTTP through `requests` or `urllib` is answered with 200 `{{"ok": true}}` and recorded (method, URL, body). `smtplib` is a recorder. S3 through boto3 is a local stand-in (moto).
  SQLite files are real. `time.sleep` is real.
- Try it: `out/{project}/.venv/bin/python tools/run_in_sandbox.py --project out/{project} --dag-id {dag_id} --spec spec.json --source source` runs your job there and prints outcomes, values, files, requests.

## Rules

1. Use the `dagster-expert` and `dagster-community-components` skills (load them by name; the second is also at `{dcc_skill}`). Search the registry (`dagster-component search ... --json`) for each stage before writing
   code, read `schema.json` before writing any component YAML, and never invent an API, a component id or a field. Record what you searched.
{rule2}
3. Preserve the behaviour under "must be preserved" in the spec: the same effects, in the same order, with the same values, failing where the original fails (a task that fails by design must still fail, and what
   ran after it must still run or not run). Where you cannot, say so in `WORKER_REPORT.md`; do not hide it.
4. No secrets, no customer names, nothing outside this folder (except `uv` and registry downloads).
5. When something in the spec is ambiguous, pick the reading the source supports, and write the assumption in `WORKER_REPORT.md`.
{rule6}
7. Count your attempts: every time you run the gates{attempt_tool} after changing code is one attempt. Report the number honestly.
8. The header docstring ends with a **decision record**: a paragraph that starts with the line `Decision record:` and one line per non-obvious behaviour, `- <behaviour>: preserved` or
   `- <behaviour>: not preserved: <why>`, for `skip/branch`, `trigger rules`, `retries`, `pools`, `sensor modes`, `depends_on_past`, `failure semantics`, `mapped tasks` (every one the spec shows). The
   `decision_record` gate checks it exists and covers them; a declared loss is accepted, a hidden one is not.
"""

RULE2_COLD = """2. Do NOT read, search or run anything under the `migrating-airflow-to-dagster` skill folder of that repository, anywhere in the repository's `examples/` or `reference/`, or any converter output. Do not look at any
   other DAG's folder or results. Work only from this folder and the two skills."""
RULE6_COLD = "6. `prompt.md` was generated for a wider workflow: it mentions a converter, a `manifest.json` and `python3 scripts/accept_rewrite.py`. None of those exist for you. There is no converter output to read, and the gate command is the one above."
# ARM C only (the converter as reference): rules 2 and 6 are relaxed for this arm and say so
RULE2_CONV = """2. **This arm (C) differs from the others: you are given the converter's project for THIS DAG as read-only reference, in `reference/converter/` (its `report.json` and its generated module). Rule 2's ban on converter output is lifted for that folder only.**
   Everything else in rule 2 still holds: do NOT read, search or run anything under the `migrating-airflow-to-dagster` skill folder of that repository, anywhere in the repository's `examples/` or `reference/` (the repository's, not this folder's), and do not look at any
   other DAG's folder or results. Work only from this folder and the two skills. The original DAG (`source/`) and `spec.md` are the source of truth; `reference/converter/` is reference material: copy logic it verified, do not inherit its structure, and do not edit it."""
RULE6_CONV = ("6. `prompt.md` was generated for a wider workflow: it mentions a `manifest.json` and `python3 scripts/accept_rewrite.py`. Neither exists for you; the gate command is the one above. **In this arm (C) there IS converter output for you to read: "
              "`reference/converter/`** (section 6 of `prompt.md` says how to use it: read-only reference, the original DAG wins). Say in the decision record what you took from it.")
REF_ROW = "| `reference/converter/` | ARM C ONLY: the converter's project for this DAG (`report.json`, the generated module, the shared runtime if any), READ-ONLY reference, not a starting point |\n"

REPORT_BEHAVIOUR_A = """## Behaviour comparison (`tools/compare_behavior.py`)
Last verdict, how many times you ran it, and for every dimension still DIFFERENT: fixed, or justified (and where in the decision record).
"""
REPORT_BEHAVIOUR_B = """## Behaviour checks you ran yourself
What you ran to see what your code does (your own tests, `tools/run_in_sandbox.py`), what they showed, and what you changed because of it.
"""
REPORT_MD = """# Worker report: `{dag_id}`

Fill in every section. Say "none" where nothing applies; do not leave a section empty.

- **Route:** rewrite (this folder's prompt; `dagster-expert` and `dagster-community-components` skills)
- **Number of attempts** (runs of the gates or the sandbox after a change):
- **Final gate result** (paste the PASS/FAIL lines):

## Files written (paths under `out/{project}/`)

## Registry components used
For each: id, the schema fields you set, validation tier (`validation.level`), and why. If none: what you searched and why nothing fit.

| id | schema fields set | tier | why |
|---|---|---|---|

## What you searched
Registry searches (the exact terms), skill references read.

## Assumptions
Every place the spec or source was ambiguous and which reading you took.

{report_behaviour}
## Behaviour you could not preserve or could not verify

## Anything else the reviewer should know
"""


FEEDBACK_A = """## Behaviour feedback: run it, read it, iterate

""" + "```\ncd out/{project} && .venv/bin/python ../../tools/compare_behavior.py --project . --spec ../../spec.json --source ../../source\n```\n\nAirflow is not needed: the original DAG was run in real Airflow when this kit was built; its results are in `tools/baseline/`. The tool runs your project in the sandbox, then prints each dimension (outcome, each\ntask's state, values, files, SQLite rows, HTTP requests, mails, S3 objects, failure behaviour, schedule, retries, pools, dependencies, job) as `match`, `DIFFERENT` or `not compared`, and for DIFFERENT\nexactly what differs. **Iterate until every DIFFERENT dimension is fixed or explicitly justified** (a `not preserved: <why>` line in the decision record and a sentence in `WORKER_REPORT.md`). If the\ntool says Airflow itself failed in the sandbox (a missing package, no network), that is not yours to fix: a rewrite that fails the same way is correct. Each run of the tool after a change is an attempt.\nAfter you finish, the harness measures the same thing and the result is what counts.\n\n"


TOOLS_TOP = ("accept_rewrite.py", "gen_tests.py", "status.py", "rewrite_patterns.py", "rewrite_project.py")
TOOLS_PARITY = ("sandbox.py", "run_dagster.py", "run_in_sandbox.py", "behavior_compare.py", "harness_cfg.py", "compare_behavior.py")


def bake_baseline(a: argparse.Namespace, work: Path, manifest: dict[str, Any], row: dict[str, Any], name: str, spec: dict[str, Any], dest: Path) -> str:
    """Put the ORIGINAL DAG's Airflow results (airflow1.json, airflow2.json: this DAG only) into tools/baseline/, scrubbed of this machine's paths. Returns how it was obtained."""
    src_dir = None
    if getattr(a, "baseline_dir", None):
        src_dir = Path(a.baseline_dir) / C.safe(row["key"]) / "out"
        if not (src_dir / "airflow1.json").exists():
            return f"none: {a.baseline_dir} has no result for this DAG"
        how = "copied from the corpus baselines (the same DAG in the same Airflow; not re-run)"
    elif getattr(a, "baseline_from", None):
        src_dir = private_dir(work, a.baseline_from) / "behavior" / name
        if not (src_dir / "airflow1.json").exists():
            return f"none: {a.baseline_from} has no airflow results for this DAG"
        how = f"copied from run {a.baseline_from} (the same DAG in the same Airflow; not re-run)"
    elif getattr(a, "venv_2", None) and getattr(a, "venv_3", None):
        import eval_behavior as E
        base = work / "baselines_kit" / C.safe(row["key"])
        E.execute(base / "out", base / "sandboxes", spec, row["dag_id"], C.dag_file(work, manifest, row), work / "repos" / row["repo"], a.venv_2 if row["airflow"] == "2" else a.venv_3, sys.executable, None, base / "none", [], 300,
                  seed=E.seed_for(C.dag_file(work, manifest, row), work / "repos" / row["repo"]))
        src_dir, how = base / "out", "run now in real Airflow (twice)"
    else:
        return "none: no --baseline-from and no Airflow interpreters were given"
    dest.mkdir(parents=True, exist_ok=True)
    for f in ("airflow1.json", "airflow2.json"):
        if (src_dir / f).exists():
            (dest / f).write_text(clean_baseline(C.scrub((src_dir / f).read_text(), work)))
    return how


def clean_baseline(text: str) -> str:
    """A raw Airflow result as a worker may see it: nothing is added or removed except that any user home path (also a truncated one in a log tail) is replaced, after `C.scrub` replaced the work directory."""
    return re.sub(r"/Users/[^/\s\"']*", "<home>", text)


def arm_text(arm: str, fmt: dict[str, str]) -> dict[str, str]:
    """The parts of TASK.md that differ between the arms. ARM C is ARM A plus `reference/converter/` (the converter's project for this DAG, reference mode) and rules 2 and 6 relaxed for it, said so in TASK.md. ARM A has the behaviour-feedback tool; ARM B has no tool that compares with Airflow and no baseline (the gates, the sandbox runner, the pattern library and every other
    instruction are identical). The `task states` gate still runs for B, by the harness after the worker finishes."""
    conv = arm == "C"
    rules = {"rule2": RULE2_CONV if conv else RULE2_COLD, "rule6": RULE6_CONV if conv else RULE6_COLD, "ref_row": REF_ROW if conv else ""}
    if arm in ("A", "C"):                                                # arm C is arm A (feedback tool and baseline) plus the converter's project as reference
        return {**rules, "tools_extra": " and `compare_behavior.py` (runs your project there and shows where it behaves differently from the original DAG in real Airflow; the original's results are in `tools/baseline/`)",
                "gate_baseline": " --baseline ../../tools/baseline --source ../../source", "states_note": " (your project's task outcomes and effects against Airflow's; it names the task that differs)",
                "feedback": FEEDBACK_A.format(**fmt), "attempt_tool": ", the sandbox or `compare_behavior.py`"}
    return {**rules, "tools_extra": "", "gate_baseline": "", "states_note": " (the harness runs it against the original DAG's Airflow results after you finish; here it reports `not run`)", "feedback": "", "attempt_tool": " or the sandbox"}


def converter_reference(conv_dir: Path, repo: str, spec: dict[str, Any], dest: Path, work: Path) -> str:
    """ARM C: copy the converter's project for THIS DAG into `dest` (reference/converter/): report.json reduced to this DAG's entry, its generated module, the shared runtime when there is one. Local paths are scrubbed.
    Looks for `<conv_dir>/<repo>.*/report.json` (or `<conv_dir>/report.json`). Returns how it was obtained; raises when no project has this DAG."""
    cands = sorted(Path(conv_dir).glob(f"{repo}.*/report.json")) + sorted(Path(conv_dir).glob("report.json"))
    for rp in cands:
        dags = json.loads(rp.read_text()).get("dags") or {}
        if spec["dag_id"] not in dags:
            continue
        src = rp.parent
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True)
        rep = json.loads(rp.read_text())
        rep["dags"] = {spec["dag_id"]: dags[spec["dag_id"]]}
        for k in [k for k in rep if k not in ("dags", "schema", "version")]:
            rep.pop(k)
        (dest / "report.json").write_text(clean_baseline(C.scrub(json.dumps(rep, indent=1, sort_keys=True), work)) + "\n")
        mods = sorted(src.glob(f"src/*/defs/{rewrite_prompt.safe_id(spec['dag_id'])}.py"))
        files = mods + sorted(src.glob("src/*/runtime.py"))
        for f in files:
            t = dest / f.relative_to(src)
            t.parent.mkdir(parents=True, exist_ok=True)
            t.write_text(clean_baseline(C.scrub(f.read_text(), work)))
        for f in [*dest.rglob("*")]:
            if f.is_file():
                f.chmod(0o444)
        return f"copied from {rp.parent.name} ({len(files)} file(s) and the DAG's report entry)"
    raise FileNotFoundError(f"no converter project under {conv_dir} has a report entry for {spec['dag_id']} (repo {repo})")


def worker_dir(work: Path, run: str, name: str) -> Path:
    return work / run / name


def private_dir(work: Path, run: str) -> Path:
    return work / f"{run}-private"


def fix_module_name(text: str, dag_id: str, module: str) -> str:
    if module == dag_id:
        return text
    return text.replace(f"defs.{dag_id}", f"defs.{module}").replace(f"defs/{dag_id}.py", f"defs/{module}.py")


def make(a: argparse.Namespace) -> int:
    work = a.work.resolve()
    manifest = C.load_manifest(a.manifest)
    sample = json.loads(a.sample.read_text())
    names = C.dir_names(sample["dags"])
    priv = private_dir(work, a.run)
    priv.mkdir(parents=True, exist_ok=True)
    dcc = a.dcc_skill or str(C.SKILL.parent / "dagster-community-components")
    index: dict[str, Any] = {}
    for e in sample["dags"]:
        row = C.row_for(manifest, e["key"])
        name = names[e["key"]]
        d = worker_dir(work, a.run, name)
        if d.exists():
            shutil.rmtree(d)
        (d / "out").mkdir(parents=True)
        (d / "source").mkdir()
        (d / "tools").mkdir()
        spec, md = C.make_spec(work, row)
        project = module = C.safe(row["dag_id"])
        project = f"rw_{project.lstrip('_')}"[:60]
        (d / "spec.json").write_text(json.dumps(spec, indent=1, sort_keys=True) + "\n")
        (d / "spec.md").write_text(md)
        arm = e.get("arm", "A")                                              # kits without an arm (earlier runs) are arm A: they always had the feedback tool
        conv_note = None
        if arm == "C":
            if not getattr(a, "converter_dir", None):
                raise SystemExit("arm C needs --converter-dir (the converter's output directories, `<repo>.<airflow>.ra/`)")
            conv_note = converter_reference(a.converter_dir, row["repo"], spec, d / "reference" / "converter", work)
            cp = rewrite_prompt.converter_start(spec, d / "reference" / "converter")
            cp["dir"] = "reference/converter"
            prompt = rewrite_prompt.render(spec, md, None, project, project, converter_project=cp, converter_mode="reference")
        else:
            prompt = rewrite_prompt.render(spec, md, None, project, project)
        (d / "prompt.md").write_text(fix_module_name(prompt, row["dag_id"], module))
        root = work / "repos" / row["repo"]
        src = C.dag_file(work, manifest, row)
        C.copy_tree_file(root, src, d / "source")
        deps = C.local_deps(root, src)
        for f in deps:
            C.copy_tree_file(root, f, d / "source")
        for f in TOOLS_TOP:
            shutil.copy2(HERE / f, d / "tools" / f)
        for f in TOOLS_PARITY:
            shutil.copy2(HERE / "parity" / f, d / "tools" / f)
        # the baseline is always obtained (the harness uses it after the worker finishes); only arm A's kit holds it
        base_note = bake_baseline(a, work, manifest, row, name, spec, priv / "baseline" / name)
        if arm in ("A", "C"):
            if (priv / "baseline" / name).exists():
                shutil.copytree(priv / "baseline" / name, d / "tools" / "baseline", dirs_exist_ok=True)
        else:
            (d / "tools" / "compare_behavior.py").unlink(missing_ok=True)
            shutil.rmtree(d / "tools" / "baseline", ignore_errors=True)
        fmt = dict(dag_id=row["dag_id"], project=project, module=module, dcc_skill=dcc)
        (d / "TASK.md").write_text(TASK_MD.format(**fmt, **arm_text(arm, fmt)))
        (d / "WORKER_REPORT.md").write_text(REPORT_MD.format(dag_id=row["dag_id"], project=project, report_behaviour=(REPORT_BEHAVIOUR_A if arm == "A" else REPORT_BEHAVIOUR_B)))
        (priv / "specs").mkdir(exist_ok=True)
        (priv / "specs" / f"{name}.spec.json").write_text(json.dumps(spec, indent=1, sort_keys=True) + "\n")
        index[e["key"]] = {"arm": arm, "group": e.get("group"), "baseline": base_note, "converter_reference": conv_note, "dir": name, "dag_id": row["dag_id"], "project": project, "module": module, "stratum": e["stratum"], "pattern": e["pattern"], "repo": row["repo"],
                           "source_file": str(src.relative_to(root)), "local_deps": [str(f.relative_to(root)) for f in deps]}
    (priv / "kit_index.json").write_text(json.dumps(index, indent=1, sort_keys=True) + "\n")
    (priv / "kit_meta.json").write_text(json.dumps({"dcc_skill": dcc, "sample_seed": sample.get("seed"), "sample_label": sample.get("label")}, indent=1) + "\n")
    (work / a.run / "README.md").write_text(f"# Rewrite kit `{a.run}`\n\nOne folder per DAG. Open only your own folder and read its `TASK.md` first. {len(index)} DAGs.\n")
    if sample.get("groups"):
        import eval_sample
        (work / a.run / "GROUPS.md").write_text(eval_sample.render_groups(sample, names))
    print(f"wrote {len(index)} worker folders under {work / a.run} and the private index in {priv}")
    problems = check_all(work, a.run, index, dcc)
    for p in problems:
        print("PROBLEM:", p)
    return 1 if problems else 0


def check_dir(d: Path, own_id: str, others: list[str], banned_paths: list[str], allow: str = "", arm: str = "A") -> list[str]:
    out: list[str] = []
    if arm != "C" and (d / "reference").exists():
        out.append(f"{d.name}: reference/ exists in a cold arm {arm} kit (only arm C holds the converter's project)")
    if arm == "C":
        rd = d / "reference" / "converter"
        if not (rd / "report.json").exists():
            out.append(f"{d.name}: arm C kit has no reference/converter/report.json")
        else:
            try:
                dags = json.loads((rd / "report.json").read_text()).get("dags") or {}
            except ValueError:
                dags = {}
            if list(dags) != [own_id]:
                out.append(f"{d.name}: reference/converter/report.json must hold exactly this DAG's entry (has {sorted(dags)})")
        if any(os.access(f, os.W_OK) for f in rd.rglob("*") if f.is_file()):
            out.append(f"{d.name}: reference/converter/ is not read-only")
    # another sampled DAG's id may legitimately appear because the worker's OWN source names it (its own file name, or a comment or `dag_id=` argument that points at another DAG): that is the worker's own
    # data, not a leak. An id that is NOT in the worker's own source files is still flagged.
    own_src = "\n".join(f.read_text(errors="ignore") + "\n" + f.name for f in (d / "source").rglob("*") if f.is_file()) if (d / "source").exists() else ""
    for f in sorted(p for p in d.rglob("*") if p.is_file()):
        rel = f.relative_to(d)
        in_ref = arm == "C" and rel.parts[:2] == ("reference", "converter")      # arm C's declared converter reference
        if f.name in FORBIDDEN_FILES and rel.parts[0] != "tools" and not in_ref:
            out.append(f"{d.name}: {rel} is converter output")
        if rel.parts[0] == "out":
            continue
        try:
            text = f.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if allow:
            text = text.replace(allow, "<dcc-skill>")
        for m in CONVERTER_MARKERS:
            if m in text and rel.parts[0] != "tools" and not in_ref and not (arm == "C" and str(rel) in ("prompt.md", "TASK.md")):
                out.append(f"{d.name}: {rel} contains the converter marker `{m}`")
        for p in banned_paths:
            if p and p in text:
                out.append(f"{d.name}: {rel} contains a local path ({p[:20]}...)")
        if rel.parts[0] in ("source",):
            continue
        for o in others:
            if o != own_id and len(o) > 6 and re.search(rf"(?<![\w-]){re.escape(o)}(?![\w-])", text) and not re.search(rf"(?<![\w-]){re.escape(o)}(?![\w-])", own_src):
                out.append(f"{d.name}: {rel} names another sampled DAG `{o}`")
    return out


def check_all(work: Path, run: str, index: dict[str, Any], dcc: str = "") -> list[str]:
    ids = sorted({v["dag_id"] for v in index.values()})
    banned = [str(work), os.path.expanduser("~"), "/Users/"]
    out: list[str] = []
    for v in index.values():
        d = worker_dir(work, run, v["dir"])
        if not d.exists():
            out.append(f"{v['dir']}: folder missing")
            continue
        out += check_dir(d, v["dag_id"], ids, banned, dcc, v.get("arm", "A"))
        out += tools_problems(d, v["dag_id"], v.get("arm", "A"), not str(v.get("baseline", "none")).startswith("none"))
        # the source folder must hold no other sampled DAG's file
        src_files = [str(p.relative_to(d / "source")) for p in (d / "source").rglob("*.py")]
        for k, o in index.items():
            if o["dag_id"] != v["dag_id"] and o["source_file"] in src_files and o["source_file"] not in [v["source_file"], *v["local_deps"]]:
                out.append(f"{v['dir']}: source/ holds the file of another sampled DAG ({o['dag_id']})")
    return out


def tools_problems(d: Path, own_id: str, arm: str = "A", baseline_expected: bool = False) -> list[str]:
    """The kit is self-contained: every module a tool imports from this skill is in tools/, and tools/baseline holds this DAG's Airflow run and nothing else."""
    import ast
    out: list[str] = []
    tools = d / "tools"
    local = {p.stem for p in list(HERE.glob("*.py")) + list((HERE / "parity").glob("*.py"))}
    have = {p.stem for p in tools.glob("*.py")}
    for need in [Path(f).stem for f in TOOLS_TOP + TOOLS_PARITY]:
        if need not in have and not (arm == "B" and need == "compare_behavior"):
            out.append(f"{d.name}: tools/{need}.py is missing")
    if arm == "B":                                                       # ARM B holds no feedback tool and no baseline, anywhere in the kit
        if "compare_behavior" in have:
            out.append(f"{d.name}: arm B kit holds tools/compare_behavior.py")
        if (tools / "baseline").exists():
            out.append(f"{d.name}: arm B kit holds tools/baseline/")
        for f in list(d.rglob("airflow1.json")) + list(d.rglob("airflow2.json")):
            if "out" not in f.relative_to(d).parts[:1]:
                out.append(f"{d.name}: arm B kit holds an Airflow result file ({f.relative_to(d)})")
        for f in ("TASK.md", "prompt.md", "WORKER_REPORT.md"):
            if (d / f).exists() and "compare_behavior" in (d / f).read_text():
                out.append(f"{d.name}: arm B {f} mentions compare_behavior")
    elif baseline_expected and not (tools / "baseline" / "airflow1.json").exists():
        out.append(f"{d.name}: arm A kit should hold tools/baseline/ (the index says one was obtained) but does not")
    for p in sorted(tools.glob("*.py")):
        try:
            tree = ast.parse(p.read_text())
        except SyntaxError:
            out.append(f"{d.name}: tools/{p.name} does not parse")
            continue
        for node in ast.walk(tree):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else ([node.module] if isinstance(node, ast.ImportFrom) and node.module and not node.level else [])
            for m in mods:
                top = m.split(".")[0]
                if top in local and top not in have and top != p.stem and not (arm == "B" and top == "compare_behavior"):      # arm B's gates never reach the lazy import (no baseline is given)
                    out.append(f"{d.name}: tools/{p.name} imports `{top}`, which is not in tools/")
    base = tools / "baseline"
    if base.exists():
        for f in base.glob("*.json"):
            try:
                j = json.loads(f.read_text())
            except ValueError:
                out.append(f"{d.name}: tools/baseline/{f.name} is not JSON")
                continue
            if j.get("dag_id") not in (None, own_id):
                out.append(f"{d.name}: tools/baseline/{f.name} is another DAG's result ({j.get('dag_id')})")
    return out


def check(a: argparse.Namespace) -> int:
    work = a.work.resolve()
    index = json.loads((private_dir(work, a.run) / "kit_index.json").read_text())
    meta = json.loads((private_dir(work, a.run) / "kit_meta.json").read_text())
    problems = check_all(work, a.run, index, meta["dcc_skill"])
    for p in problems:
        print("PROBLEM:", p)
    arms = {x: sum(1 for v in index.values() if v.get("arm", "A") == x) for x in ("A", "B", "C")}
    if problems:
        verdict = f"{len(problems)} problem(s)"
    else:
        verdict = "clean (no converter output outside arm C's reference/converter, no other DAG data, no local path" + ("; every arm B kit holds no feedback tool and no baseline" if arms["B"] else "") \
                  + ("; every arm C kit holds only its own DAG's converter project, read-only, under reference/converter/" if arms["C"] else "") + ")"
    print(f"checked {len(index)} worker folders (arm A {arms['A']}, arm B {arms['B']}, arm C {arms['C']}): {verdict}")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make")
    m.add_argument("--work", type=Path, required=True)
    m.add_argument("--manifest", type=Path, required=True)
    m.add_argument("--sample", type=Path, required=True)
    m.add_argument("--run", required=True)
    m.add_argument("--dcc-skill")
    m.add_argument("--converter-dir", type=Path, help="ARM C only (a sample entry with `\"arm\": \"C\"`): the converter's output directories (`<dir>/<repo>.<airflow>.ra/report.json` + `src/`); each arm C kit gets that DAG's project, read-only, under reference/converter/. Cold arms never get it")
    m.add_argument("--baseline-dir", type=Path, help="a folder of per-DAG baselines `<dir>/<repo>__<dag>/out/airflow{1,2}.json` (eval_baseline.py's output, `<work>/baselines`): copied, not re-run")
    m.add_argument("--baseline-from", help="an earlier run of the same DAGs whose raw Airflow results to bake into each kit (its <work>/<RUN>-private/behavior/<dag>/airflow{1,2}.json)")
    m.add_argument("--venv-2")
    m.add_argument("--venv-3")
    c = sub.add_parser("check")
    c.add_argument("--work", type=Path, required=True)
    c.add_argument("--run", required=True)
    a = ap.parse_args(argv)
    return make(a) if a.cmd == "make" else check(a)


if __name__ == "__main__":
    raise SystemExit(main())
