#!/usr/bin/env python3
"""Run the acceptance gates (`accept_rewrite.py`) on every worker project of a run, and read each WORKER_REPORT.md.

    python3 eval_gates.py --work DIR --run stage1 [--only dag_id ...] [--jobs 4] [--dagster-python PY]

For each DAG folder `<work>/<run>/<dag>/` that has `out/<project>/`: runs the gates with the project's own virtualenv (falling back to --dagster-python), WITHOUT the `parity` baseline (the gate reports `parity: not run (no baseline)`); the `task_states` gate uses the kit's own `tools/baseline/` when there is one, and writes `<work>/<run>-private/gates/<dag>.gates.json` plus
`<dag>.report.json` (what the worker reported: attempts, registry components, assumptions section present). A folder with no project is recorded as `no project`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import eval_common as C
import rewrite_project as RPJ


def parse_report(text: str) -> dict[str, Any]:
    m = re.search(r"Number of attempts[^:]*:\*?\*?\s*(\d+)", text)
    rows = [ln for ln in text.splitlines() if ln.startswith("|") and not re.match(r"^\|[\s\-|:]+\|$", ln) and "schema fields set" not in ln]
    comps = [r.split("|")[1].strip() for r in rows if len(r.split("|")) > 2 and r.split("|")[1].strip() and r.split("|")[1].strip().lower() not in ("none", "id")]
    filled = {}
    for sec in ("Files written", "Registry components used", "What you searched", "Assumptions", "Behaviour comparison", "Behaviour you could not preserve or could not verify"):
        mm = re.search(rf"## {re.escape(sec)}[^\n]*\n(.*?)(?=\n## |\Z)", text, re.S)
        body = re.sub(r"\|[\s\-|:]*\|\n?|\|.*?\|\n?", "", mm.group(1)).strip() if mm else ""
        filled[sec] = bool(body) and not body.lower().startswith(("for each:", "every place", "registry searches"))
    return {"attempts": int(m.group(1)) if m else None, "registry_components": comps, "sections_filled": filled, "words": len(text.split()), **searches_reported(text)}


def searches_reported(text: str) -> dict[str, Any]:
    """Does the worker report say registry searches were run? Read from the `What you searched` section: it names `dagster-component search` or a quoted search term, and does not say none / not run. `search_terms` counts the quoted terms."""
    mm = re.search(r"## What you searched[^\n]*\n(.*?)(?=\n## |\Z)", text, re.S)
    body = mm.group(1).strip() if mm else ""
    if not body or body.lower().startswith(("registry searches", "for each:")):
        return {"searches_reported": False, "search_terms": 0}
    terms = re.findall(r"[\"“`]([^\"”`\n]{3,80})[\"”`]", body)
    terms = [t for t in terms if not t.startswith(("dagster-component", "dagster-", "dg "))]
    low = body.lower()
    said_none = bool(re.match(r"(none|no searches?|not run|did not search)\b", low))
    ran = ("search" in low) and not said_none
    return {"searches_reported": ran, "search_terms": len(terms) if ran else 0}


def one(a: argparse.Namespace, key: str, v: dict[str, Any]) -> dict[str, Any]:
    work = a.work.resolve()
    d = work / a.run / v["dir"]
    priv = work / f"{a.run}-private"
    (priv / "gates").mkdir(parents=True, exist_ok=True)
    rep = d / "WORKER_REPORT.md"
    (priv / "gates" / f"{v['dir']}.report.json").write_text(json.dumps(parse_report(rep.read_text()) if rep.exists() else {"missing": True}, indent=1) + "\n")
    roots = [p for p in sorted((d / "out").iterdir()) if (p / "src").is_dir()] if (d / "out").exists() else []
    project = roots[0] if roots else d / "out" / v["project"]            # the folder on disk: `uv` may have normalised the name the index computed (`rw_x_S3` -> `rw_x_s3`)
    out = priv / "gates" / f"{v['dir']}.gates.json"
    if not (project / "src").exists():
        out.write_text(json.dumps({"dag_id": v["dag_id"], "passed": False, "no_project": True, "gates": []}, indent=1) + "\n")
        return {"key": key, "passed": False, "no_project": True}
    pkg = RPJ.package_of(project, v["project"])
    rep_json = json.loads((priv / "gates" / f"{v['dir']}.report.json").read_text())
    rep_json["components"] = RPJ.component_usage(project, pkg)           # what the project USES (read from its defs.yaml files), whatever the worker reported
    rep_json["style"] = "component" if RPJ.component_dirs(project, pkg) else "module"
    (priv / "gates" / f"{v['dir']}.report.json").write_text(json.dumps(rep_json, indent=1) + "\n")
    py = project / ".venv" / "bin" / "python"
    spec = d / "spec.json"
    cmd = [str(py) if py.exists() else a.dagster_python, str(C.HERE / "accept_rewrite.py"), "--spec", str(spec), "--project", str(project), "--out", str(out)]
    # the Airflow baseline for the task_states gate: the kit's own (arm A) or the harness's private copy (arm B kits hold none: the worker never sees it)
    for bdir in (d / "tools" / "baseline", priv / "baseline" / v["dir"]):
        if (bdir / "airflow1.json").exists():
            cmd += ["--baseline", str(bdir), "--source", str(d / "source")]
            break
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=a.timeout, env={**{k: x for k, x in os.environ.items() if k != "VIRTUAL_ENV"}, "PYTHONPATH": str(C.HERE)}, cwd=str(project))
    except subprocess.TimeoutExpired:
        out.write_text(json.dumps({"dag_id": v["dag_id"], "passed": False, "gates": [{"gate": "harness", "passed": False, "detail": f"timeout after {a.timeout}s"}]}, indent=1) + "\n")
        return {"key": key, "passed": False}
    if not out.exists():
        out.write_text(json.dumps({"dag_id": v["dag_id"], "passed": False, "gates": [{"gate": "harness", "passed": False, "detail": (r.stderr or r.stdout)[-300:]}]}, indent=1) + "\n")
    return {"key": key, "passed": json.loads(out.read_text())["passed"]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--run", required=True)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--dagster-python", default=os.environ.get("EVAL_DAGSTER_PYTHON", sys.executable))
    a = ap.parse_args(argv)
    index = json.loads((a.work.resolve() / f"{a.run}-private" / "kit_index.json").read_text())
    todo = [(k, v) for k, v in sorted(index.items()) if not a.only or v["dag_id"] in a.only]
    with ThreadPoolExecutor(a.jobs) as ex:
        res = list(ex.map(lambda kv: one(a, *kv), todo))
    print(f"gates: {sum(r['passed'] for r in res)} of {len(res)} DAGs pass every gate; {sum(1 for r in res if r.get('no_project'))} have no project")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
