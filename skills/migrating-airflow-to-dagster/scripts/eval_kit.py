#!/usr/bin/env python3
"""Generate the cold-worker kit: for each sampled DAG, a folder with exactly what a fresh agent needs to write the rewrite, and nothing else.

    python3 eval_kit.py make  --work DIR --manifest corpus/manifest.json --sample sample.json --run stage1 [--dcc-skill PATH]
    python3 eval_kit.py check --work DIR --run stage1 [--manifest corpus/manifest.json --sample sample.json]

`make` writes, per DAG, DIR/<run>/<dag>/ :
    TASK.md             what to produce and where, the gates, the sandbox contract, the rules
    WORKER_REPORT.md    the template the worker fills in (route, files, registry components, assumptions, gaps, attempts)
    spec.md, spec.json  the facts-only spec (`dag_spec.py`), generated WITHOUT any converter data and WITHOUT an Airflow baseline
    prompt.md           the generated rewrite prompt (`rewrite_prompt.py`), unchanged except the module name when the DAG id is not a Python identifier
    source/             the original DAG file and its local dependencies (modules it imports, data files it names), at their repository paths
    tools/              the acceptance gates (accept_rewrite.py and the two modules it imports) and a sandbox runner, so the worker can check its own work
    out/                empty: the worker creates its Dagster project here
and DIR/<run>-private/ (kit index, private specs): never given to workers.

`check` fails when a worker folder holds converter output (a converted module, the shared runtime, a conversion report, a caveat marker) or any other sampled DAG's data (its id, its source file,
its spec), or a path of this machine's user. It is the guard that the rewrites are cold.
"""
from __future__ import annotations

import argparse
import json
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
| `tools/` | `accept_rewrite.py` (the acceptance gates) and `run_in_sandbox.py` (runs your DAG in the same sandbox the measurement uses) |
| `out/` | where your Dagster project goes |
| `WORKER_REPORT.md` | fill it in when you finish |

## What to produce

1. A Dagster project under `out/{project}/`, created with `cd out && uvx create-dagster project {project} --uv-sync` (never by hand), so it has its own `.venv` with Dagster. Add what your code needs with
   `uv add` (for example `pytest`, `requests`, `moto[s3]`) inside the project.
2. The rewrite in `out/{project}/src/{project}/defs/{module}.py` (plus component folders under `src/{project}/defs/` if you use registry components). Its first lines are the header docstring that accounts
   for every Airflow task (`prompt.md`, section 4: `<task_id>: replaced by asset `<key>`` / `folded into `<key>`` / `merged into `<key>`` / `dropped: <reason>`). A job named `{dag_id}` over the DAG's assets,
   and the schedule from the spec.
3. Your own tests under `out/{project}/tests/` that check what the DAG does (values, files, rows, requests it makes), not only that it loads.
4. `WORKER_REPORT.md` filled in.

## The acceptance gates (you run them; they are necessary, not sufficient)

```
cd out/{project} && .venv/bin/python ../../tools/accept_rewrite.py --spec ../../spec.json --module {project}.defs.{module} --project . [--defs-dir src/{project}/defs/<component folder>]
```

Seven gates: `dg check defs`, a generated behavioural test, asset checks, parity (not run here: no Airflow baseline is given to you), no secret values, no invented APIs, every task accounted for.
Passing them does not mean the behaviour is right. After the gates, the harness runs the original DAG in Airflow and your project side by side and compares outcomes, values, files, rows, requests, mails,
schedule, retries, pools and dependencies. You will not see those results.

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
2. Do NOT read, search or run anything under the `migrating-airflow-to-dagster` skill folder of that repository, anywhere in the repository's `examples/` or `reference/`, or any converter output. Do not look at any
   other DAG's folder or results. Work only from this folder and the two skills.
3. Preserve the behaviour under "must be preserved" in the spec: the same effects, in the same order, with the same values, failing where the original fails (a task that fails by design must still fail, and what
   ran after it must still run or not run). Where you cannot, say so in `WORKER_REPORT.md`; do not hide it.
4. No secrets, no customer names, nothing outside this folder (except `uv` and registry downloads).
5. When something in the spec is ambiguous, pick the reading the source supports, and write the assumption in `WORKER_REPORT.md`.
6. `prompt.md` was generated for a wider workflow: it mentions a converter, a `manifest.json` and `python3 scripts/accept_rewrite.py`. None of those exist for you. There is no converter output to read, and the gate command is the one above.
7. Count your attempts: every time you run the gates or the sandbox after changing code is one attempt. Report the number honestly.
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

## Behaviour you could not preserve or could not verify

## Anything else the reviewer should know
"""


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
        prompt = rewrite_prompt.render(spec, md, None, project, project)
        (d / "prompt.md").write_text(fix_module_name(prompt, row["dag_id"], module))
        root = work / "repos" / row["repo"]
        src = C.dag_file(work, manifest, row)
        C.copy_tree_file(root, src, d / "source")
        deps = C.local_deps(root, src)
        for f in deps:
            C.copy_tree_file(root, f, d / "source")
        for f in ("accept_rewrite.py", "gen_tests.py", "status.py"):
            shutil.copy2(HERE / f, d / "tools" / f)
        shutil.copy2(HERE / "parity" / "sandbox.py", d / "tools" / "sandbox.py")
        shutil.copy2(HERE / "parity" / "run_dagster.py", d / "tools" / "run_dagster.py")
        shutil.copy2(HERE / "parity" / "run_in_sandbox.py", d / "tools" / "run_in_sandbox.py")
        (d / "TASK.md").write_text(TASK_MD.format(dag_id=row["dag_id"], project=project, module=module, dcc_skill=dcc))
        (d / "WORKER_REPORT.md").write_text(REPORT_MD.format(dag_id=row["dag_id"], project=project))
        (priv / "specs").mkdir(exist_ok=True)
        (priv / "specs" / f"{name}.spec.json").write_text(json.dumps(spec, indent=1, sort_keys=True) + "\n")
        index[e["key"]] = {"dir": name, "dag_id": row["dag_id"], "project": project, "module": module, "stratum": e["stratum"], "pattern": e["pattern"], "repo": row["repo"],
                           "source_file": str(src.relative_to(root)), "local_deps": [str(f.relative_to(root)) for f in deps]}
    (priv / "kit_index.json").write_text(json.dumps(index, indent=1, sort_keys=True) + "\n")
    (priv / "kit_meta.json").write_text(json.dumps({"dcc_skill": dcc, "sample_seed": sample.get("seed"), "sample_label": sample.get("label")}, indent=1) + "\n")
    (work / a.run / "README.md").write_text(f"# Rewrite kit `{a.run}`\n\nOne folder per DAG. Open only your own folder and read its `TASK.md` first. {len(index)} DAGs.\n")
    print(f"wrote {len(index)} worker folders under {work / a.run} and the private index in {priv}")
    problems = check_all(work, a.run, index, dcc)
    for p in problems:
        print("PROBLEM:", p)
    return 1 if problems else 0


def check_dir(d: Path, own_id: str, others: list[str], banned_paths: list[str], allow: str = "") -> list[str]:
    out: list[str] = []
    for f in sorted(p for p in d.rglob("*") if p.is_file()):
        rel = f.relative_to(d)
        if f.name in FORBIDDEN_FILES and rel.parts[0] != "tools":
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
            if m in text and rel.parts[0] != "tools":
                out.append(f"{d.name}: {rel} contains the converter marker `{m}`")
        for p in banned_paths:
            if p and p in text:
                out.append(f"{d.name}: {rel} contains a local path ({p[:20]}...)")
        if rel.parts[0] in ("source",):
            continue
        for o in others:
            if o != own_id and len(o) > 6 and re.search(rf"(?<![\w-]){re.escape(o)}(?![\w-])", text):
                out.append(f"{d.name}: {rel} names another sampled DAG `{o}`")
    return out


def check_all(work: Path, run: str, index: dict[str, Any], dcc: str = "") -> list[str]:
    import os
    ids = sorted({v["dag_id"] for v in index.values()})
    banned = [str(work), os.path.expanduser("~"), "/Users/"]
    out: list[str] = []
    for v in index.values():
        d = worker_dir(work, run, v["dir"])
        if not d.exists():
            out.append(f"{v['dir']}: folder missing")
            continue
        out += check_dir(d, v["dag_id"], ids, banned, dcc)
        # the source folder must hold no other sampled DAG's file
        src_files = [str(p.relative_to(d / "source")) for p in (d / "source").rglob("*.py")]
        for k, o in index.items():
            if o["dag_id"] != v["dag_id"] and o["source_file"] in src_files and o["source_file"] not in [v["source_file"], *v["local_deps"]]:
                out.append(f"{v['dir']}: source/ holds the file of another sampled DAG ({o['dag_id']})")
    return out


def check(a: argparse.Namespace) -> int:
    work = a.work.resolve()
    index = json.loads((private_dir(work, a.run) / "kit_index.json").read_text())
    meta = json.loads((private_dir(work, a.run) / "kit_meta.json").read_text())
    problems = check_all(work, a.run, index, meta["dcc_skill"])
    for p in problems:
        print("PROBLEM:", p)
    print(f"checked {len(index)} worker folders: {'clean (no converter output, no other DAG data, no local path)' if not problems else str(len(problems)) + ' problem(s)'}")
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
    c = sub.add_parser("check")
    c.add_argument("--work", type=Path, required=True)
    c.add_argument("--run", required=True)
    a = ap.parse_args(argv)
    return make(a) if a.cmd == "make" else check(a)


if __name__ == "__main__":
    raise SystemExit(main())
