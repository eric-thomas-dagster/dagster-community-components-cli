"""Behaviour feedback for a rewrite worker: run YOUR project in the measurement's sandbox and compare it with what the original DAG did in real Airflow.

    python tools/compare_behavior.py --project out/<project> --spec spec.json --source source [--baseline tools/baseline] [--dag-id <id>]

Run it with your project's interpreter (`out/<project>/.venv/bin/python tools/compare_behavior.py ...`). Airflow is NOT needed: the original DAG was run in real Airflow (twice) when your kit was built and
its results are in `tools/baseline/` (this DAG only). Your project is run in the same sandbox the measurement uses (scratch working directory with your `source/` files, SQLite, HTTP and SMTP
recorders, S3 stand-in, no network), then compared dimension by dimension: overall outcome, each task's outcome, values, files, SQLite rows, HTTP requests, mails, S3 objects, failure behaviour,
and schedule, retries, pools, dependencies, job.

Every dimension prints `match`, `DIFFERENT` or `not compared` and, for DIFFERENT, exactly what differs. Iterate until each DIFFERENT dimension is fixed or explicitly justified in the header's decision
record. Some differences are not yours: if Airflow itself failed in the sandbox (a missing package, no network) the tool says so and a rewrite that fails the same way is correct. Exit code 0: no
behavioural dimension differs; 1: at least one does; 2: no baseline is available.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
if (HERE.parent / "rewrite_project.py").exists():                       # the repository layout; in a worker kit everything is flat in tools/
    sys.path.insert(0, str(HERE.parent))
import behavior_compare as B  # noqa: E402
from harness_cfg import sandbox_cfg  # noqa: E402


def load_baseline(d: Path) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    f1, f2 = Path(d) / "airflow1.json", Path(d) / "airflow2.json"
    return (json.loads(f1.read_text()) if f1.exists() else None), (json.loads(f2.read_text()) if f2.exists() else None)


def run_rewrite(project: Path, spec: dict[str, Any], dag_id: str, source: Path | None, python: str | None = None, timeout: int = 300) -> dict[str, Any]:
    """Run the project's job in a fresh sandbox exactly as the measurement does and return `run_dagster.py`'s result."""
    project = Path(project).resolve()
    root = Path(tempfile.mkdtemp(prefix="compare_sandbox_"))
    seed = {str(f.relative_to(source)): str(f) for f in sorted(Path(source).rglob("*")) if f.is_file()} if source and Path(source).exists() else {}
    cfg = {"kind": "rewrite", "dag_id": dag_id, "repo_root": None, "sandbox": sandbox_cfg(root, spec, None, seed), "out": str(root / "result.json"), "timeout_s": timeout, "rewrite": {"project": str(project), "module": None}}
    (root / "config.json").write_text(json.dumps(cfg))
    venv_py = project / ".venv" / "bin" / "python"
    py = python or (str(venv_py) if venv_py.exists() else sys.executable)
    env = {**os.environ, "PYTHONWARNINGS": "ignore", "PATH": str(Path(py).parent) + os.pathsep + os.environ.get("PATH", "")}
    proc_out = ""
    try:
        proc_out = subprocess.run([py, "-W", "ignore", str(HERE / "run_dagster.py"), str(root / "config.json")], capture_output=True, text=True, timeout=timeout + 60, env=env).stdout
    except subprocess.TimeoutExpired:
        pass
    try:
        if (root / "result.json").exists():
            return {**json.loads((root / "result.json").read_text()), "stdout_lines": B.printed_lines(proc_out)}
        return {"error": "the run left no result", "state": None}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def compare_project(project: Path, spec: dict[str, Any], baseline_dir: Path, source: Path | None, python: str | None = None, timeout: int = 300) -> dict[str, Any] | None:
    a1, a2 = load_baseline(baseline_dir)
    if a1 is None:
        return None
    route = run_rewrite(project, spec, spec["dag_id"], source, python, timeout)
    import rewrite_project as RPJ
    tm, _ = B.rewrite_taskmap(RPJ.sources(Path(project)), spec, route)
    res = B.compare(spec, a1, a2 if a2 and (a2.get("state") is not None or a2.get("error") is None) else None, route, tm, "rewrite")
    res["taskmap"], res["route_error"], res["route_notes"] = tm, route.get("error"), route.get("notes", [])
    return res


HINTS = {
    "outcome": "the run ended differently from Airflow's run",
    "tasks": "a task ended in a different state from Airflow's (success / failed / skipped / upstream_failed); a task folded into another asset is judged by its asset",
    "values": "a value Airflow returned (XCom) differs from the asset's value",
    "files": "files created or changed differ (name or content hash)",
    "sqlite": "SQLite rows differ", "http": "HTTP requests sent differ (method, URL, body)", "smtp": "mails sent differ", "s3": "S3 objects differ",
    "stdout": "a line Airflow printed (masked of times, ids, paths) is not printed by your run; extra lines are fine",
    "failure": "which tasks failed or were upstream_failed differs", "schedule": "schedule differs", "retries": "retry count or delay differs", "pools": "pool differs",
    "dependencies": "ordering between tasks differs", "job": "no job named after the DAG",
}


def render(res: dict[str, Any]) -> str:
    L = []
    ro = res["rollup"]
    L.append(f"VERDICT: {ro['verdict']}  ({ro.get('why')})")
    b = res["baseline"]
    L.append(f"Airflow baseline: {b['class']}: {b['reason']}")
    if res.get("route_error"):
        L.append(f"YOUR RUN FAILED TO EXECUTE: {res['route_error']}")
    L.append("")
    for n, d in res["dimensions"].items():
        tag = {"match": "match", "differ": "DIFFERENT", "not-compared": "not compared"}[d["verdict"]]
        L.append(f"[{tag:12}] {n}" + (f": {d.get('reason')}" if d["verdict"] == "not-compared" and d.get("reason") else ""))
        if d["verdict"] != "differ":
            for a in (d.get("evidence") or {}).get("accepted_representation", []):
                L.append(f"               accepted: {a['task']} (Airflow skipped, folded into `{a['asset']}`): {a['why']}")
            continue
        L.append(f"               {HINTS.get(n, '')}")
        ev = d.get("evidence") or {}
        if n == "tasks":
            for x in ev.get("differences", []):
                L.append(f"               - task `{x['task']}`: Airflow {x['airflow']}, yours {x['route']}" + (f" (asset `{x['asset']}`)" if x.get("asset") else ""))
            for a in ev.get("accepted_representation", []):
                L.append(f"               accepted: {a['task']}: folded into `{a['asset']}` (Airflow skipped)")
        elif n in ("values",):
            for x in ev.get("differences", []):
                L.append(f"               - `{x['task']}`: Airflow {x['airflow'][:100]} vs yours {str(x['route'])[:100]}" + ("" if x.get("consumed") else " (nothing consumes it)"))
        else:
            for k, v in ev.items():
                L.append(f"               {k}: {json.dumps(v, default=str)[:600]}")
    L += ["", "Fix each DIFFERENT dimension, or justify it in the header's decision record (`Decision record:` section) with `not preserved: <why>`."]
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", type=Path, required=True)
    ap.add_argument("--spec", type=Path, required=True)
    ap.add_argument("--source", type=Path)
    ap.add_argument("--baseline", type=Path, default=HERE / "baseline")
    ap.add_argument("--dag-id")
    ap.add_argument("--python", help="interpreter to run your project with (default: the project's .venv, else this one)")
    ap.add_argument("--timeout", type=int, default=300)
    a = ap.parse_args(argv)
    spec = json.loads(a.spec.read_text())
    if a.dag_id and a.dag_id != spec["dag_id"]:
        print(f"--dag-id {a.dag_id} does not match the spec ({spec['dag_id']})")
        return 2
    res = compare_project(a.project, spec, a.baseline, a.source, a.python, a.timeout)
    if res is None:
        print(f"no Airflow baseline in {a.baseline}: this kit has none for the DAG, so there is nothing to compare with")
        return 2
    print(render(res))
    return 1 if res["rollup"]["verdict"] == "different" else 0


if __name__ == "__main__":
    raise SystemExit(main())
