#!/usr/bin/env python3
"""Reconcile a static manifest with the DAG and task lists from a real Airflow parse (airflow_truth.py).

    python3 reconcile.py manifest.json truth.json [--out manifest.json]

For everything Airflow knows and the scan does not, a unit is added (origin "airflow_runtime", class JUDG)
so nothing is silently omitted. Static units that Airflow did not produce are kept and annotated, since the
usual cause is a file Airflow could not import (listed in `import_errors`). The report separates:

  exact      the task id matches
  covered    the DAG has a unit flagged dynamic_id / in_loop / mapped that plausibly accounts for it
  added      no static unit accounts for it: now added from the runtime view
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def reconcile(manifest: dict, truth: dict) -> dict:
    units = manifest["units"]
    dag_units = {u["dag_id"]: u for u in units if u["kind"] in ("dag", "cosmos", "dag_factory")}
    tasks: dict[str, list[dict]] = {}
    for u in units:
        if u["kind"] == "task":
            tasks.setdefault(u["dag_id"], []).append(u)
    n = len(units)
    stats = {"dags_added": 0, "tasks_exact": 0, "tasks_covered": 0, "tasks_added": 0, "static_dags_unconfirmed": []}

    def new_id() -> str:
        nonlocal n
        n += 1
        return f"A{n:03d}"

    for dag_id, t in truth["dags"].items():
        if dag_id not in dag_units:
            units.append({"id": new_id(), "kind": "dag", "dag_id": dag_id, "file": t["file"], "line": 0,
                          "origin": "airflow_runtime", "schedule": {"kind": "runtime", "raw": t["schedule"]},
                          "catchup": None, "max_active_runs": None, "tags": None, "default_args": {}, "edges": [],
                          "signals": {}, "task_ids": t["tasks"], "classification": "JUDG",
                          "classification_reason": "found only by Airflow's parser: a construct the static scan does not model",
                          "status": "pending", "evidence": "", "reason": "", "target": {}})
            stats["dags_added"] += 1
        have = tasks.get(dag_id, [])
        consts = {u["task_id"] for u in have if not (u.get("dynamic_id") or u.get("in_loop"))}
        flagged = any(u.get("dynamic_id") or u.get("in_loop") or u.get("mapped") for u in have)
        for tid in t["tasks"]:
            if tid in consts:
                stats["tasks_exact"] += 1
            elif flagged:
                stats["tasks_covered"] += 1
            else:
                units.append({"id": new_id(), "kind": "task", "dag_id": dag_id, "task_id": tid, "file": t["file"], "line": 0,
                              "origin": "airflow_runtime", "operator": "unknown (runtime-only)", "style": "runtime",
                              "pool": None, "retries": None, "trigger_rule": None, "mapped": False, "dynamic_id": False,
                              "in_loop": False, "implicit_xcom": False, "upstream": [], "outlets": "", "inlets": "", "signals": {},
                              "classification": "JUDG",
                              "classification_reason": "found only by Airflow's parser: read the source and add it to the plan",
                              "status": "pending", "evidence": "", "reason": "", "target": {}})
                stats["tasks_added"] += 1
    for dag_id, u in dag_units.items():
        if dag_id not in truth["dags"]:
            stats["static_dags_unconfirmed"].append(dag_id)
            u.setdefault("notes", []).append("not produced by Airflow's parser (import error, optional dependency, or a dead file)")
    manifest["reconciled_with"] = {"import_errors": truth.get("import_errors", {})}
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("manifest", type=Path)
    ap.add_argument("truth", type=Path)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    m = json.loads(a.manifest.read_text())
    stats = reconcile(m, json.loads(a.truth.read_text()))
    (a.out or a.manifest).write_text(json.dumps(m, indent=2, default=str) + "\n")
    print(f"tasks: {stats['tasks_exact']} exact, {stats['tasks_covered']} covered by flagged dynamic/loop units, "
          f"{stats['tasks_added']} added from the runtime view; DAGs added: {stats['dags_added']}")
    if stats["static_dags_unconfirmed"]:
        print(f"{len(stats['static_dags_unconfirmed'])} static DAG(s) Airflow did not produce: {', '.join(sorted(stats['static_dags_unconfirmed'])[:8])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
