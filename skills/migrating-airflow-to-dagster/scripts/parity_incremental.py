#!/usr/bin/env python3
"""Parity between Airflow and the converted Dagster project, running only what changed.

    python3 parity_incremental.py CONFIG.json [--all]      select changed DAGs, run both sides, merge, compare
    python3 parity_incremental.py CONFIG.json --seed       record the current conversion as already checked
    python3 parity_incremental.py CONFIG.json --select     only list what would run

A DAG is re-run when the hash of its generated module differs from the hash recorded at its last parity run (or it has none).
`--all` re-runs everything: use it after changing something the DAGs share (the generated runtime module). Results from earlier
runs stay in the state directory, so the comparison always covers every DAG that has ever been run.

CONFIG.json
  {"state_dir": "parity_state",
   "dagster_python": "/path/to/python with dagster and the converted project's dependencies",
   "package": "pkg",                                  (the package that holds defs/, default pkg)
   "repos": [{"name": "my-dags",
              "airflow_python": "/path/to/python with Airflow",
              "airflow_repo_root": "/path/to/repo",   (put on the path for the DAG files' own imports)
              "dag_folders": ["/path/to/repo/dags"],
              "converted_src": "conv/my-dags/src",    (the directory that contains the package)
              "report": "conv/my-dags/report.json",
              "ids": "conv/my-dags/ids.json"}]}      (the DAGs to compare: converted, no external systems, no long waits)

Each side runs in its own interpreter because Airflow and Dagster usually cannot share an environment.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent


def safe(n: str) -> str:
    s = re.sub(r"\W", "_", n)
    return f"_{s}" if s[:1].isdigit() else s


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    cfg = json.loads(Path(args[0]).read_text())
    state = Path(cfg.get("state_dir", "parity_state")); state.mkdir(parents=True, exist_ok=True)
    pkg = cfg.get("package", "pkg")
    hpath = state / "hashes.json"
    hashes = json.loads(hpath.read_text()) if hpath.exists() else {}

    def dag_hash(r: dict, did: str) -> str | None:
        p = Path(r["converted_src"]) / pkg / "defs" / f"{safe(did)}.py"
        return hashlib.sha1(p.read_bytes()).hexdigest() if p.exists() else None

    if "--seed" in flags:
        for r in cfg["repos"]:
            for did in json.loads(Path(r["ids"]).read_text()):
                hashes[f"{r['name']}/{did}"] = dag_hash(r, did)
        hpath.write_text(json.dumps(hashes)); print("recorded", len(hashes)); return 0
    plan = {}
    for r in cfg["repos"]:
        ids = json.loads(Path(r["ids"]).read_text())
        plan[r["name"]] = [d for d in ids if "--all" in flags or hashes.get(f"{r['name']}/{d}") != dag_hash(r, d)]
    print("to run:", {k: len(v) for k, v in plan.items() if v} or "nothing (every DAG's generated code is unchanged since its last run)")
    if "--select" in flags:
        return 0
    new = Path(tempfile.mkdtemp(prefix="parity_new_"))
    for r in cfg["repos"]:
        todo = plan[r["name"]]
        if not todo:
            continue
        ids_file = new / f"ids_{r['name']}.json"; ids_file.write_text(json.dumps(todo))
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([r.get("airflow_repo_root", ""), os.environ.get("PYTHONPATH", "")])}
        subprocess.run([r["airflow_python"], str(HERE / "parity" / "parity_af.py"), r.get("airflow_repo_root", "."), os.pathsep.join(r["dag_folders"]).replace(os.pathsep, ":"),
                        str(ids_file), str(new / f"af_{r['name']}.json")], env=env, check=False, capture_output=True)
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([r["converted_src"], r.get("airflow_repo_root", "")]), "PARITY_PKG": pkg}   # the DAGs' own local modules (include/) are in the repo
        subprocess.run([cfg["dagster_python"], str(HERE / "parity" / "parity_dg.py"), r["converted_src"], str(ids_file), r["report"],
                        str(new / f"dg_{r['name']}.json")], env=env, check=False, capture_output=True)
    for side in ("af", "dg"):
        for f in new.glob(f"{side}_*.json"):
            merged = state / f.name
            data = json.loads(merged.read_text()) if merged.exists() else {}
            data.update(json.loads(f.read_text())); merged.write_text(json.dumps(data, indent=1))
    for r in cfg["repos"]:
        f = new / f"dg_{r['name']}.json"
        if f.exists():
            for did in json.loads(f.read_text()):
                hashes[f"{r['name']}/{did}"] = dag_hash(r, did)
    hpath.write_text(json.dumps(hashes))
    (state / "reports.json").write_text(json.dumps({r["name"]: str(Path(r["report"]).resolve()) for r in cfg["repos"]}))
    return subprocess.run([sys.executable, str(HERE / "parity" / "compare.py"), str(state)]).returncode


if __name__ == "__main__":
    raise SystemExit(main())
