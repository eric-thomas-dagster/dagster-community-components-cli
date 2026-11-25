#!/usr/bin/env python3
"""Record the converter's outcome in the migration manifest.

    python3 apply_conversion.py manifest.json ir.json report.json [--out manifest.json]

For every DAG convert_ir.py converted, each manifest unit gets a `target` (assets, dependencies, job, schedule) computed from
the IR, so `validate_defs.py` can check that the code that was GENERATED loads and matches. For every DAG it deferred, the
DAG and its task units are deferred with the converter's reason. Units the converter cannot place (for example a loop-
generated task id the static scan only knows as a placeholder) stay pending and are listed.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import convert_ir  # noqa: E402
import status  # noqa: E402

ELIDED = {"EmptyOperator", "DummyOperator"}


def safe(name: str) -> str:
    s = re.sub(r"\W", "_", name)
    return f"_{s}" if s[:1].isdigit() else s


def apply(manifest: dict, ir: dict, report: dict) -> dict[str, int]:
    convert_ir.DATASET_KEYS = report.get("dataset_keys", "path")
    dags = {d["dag_id"]: d for d in ir["dags"]}
    units = manifest["units"]
    by_dag = {u["dag_id"]: u for u in units if u["kind"] in ("dag", "cosmos", "dag_factory", "blueprint_dag") and not u.get("factory")}
    by_task = {(u["dag_id"], u["task_id"]): u for u in units if u["kind"] == "task"}
    stats = {"dags_planned": 0, "dags_deferred": 0, "tasks_planned": 0, "tasks_deferred": 0, "left_pending": 0}
    for dag_id, rep in report["dags"].items():
        d = dags.get(dag_id)
        if d is None:
            continue
        if rep["status"] == "deferred":
            reason = f"convert_ir deferred this DAG: {rep['reason']}"[:900]
            for u in [by_dag.get(dag_id), *[by_task.get((dag_id, t["task_id"])) for t in d["tasks"]]]:
                if u is not None and u["status"] == "pending":
                    status.defer(manifest, u["id"], reason)
                    stats["dags_deferred" if u["kind"] != "task" else "tasks_deferred"] += 1
            continue
        tasks = {t["task_id"]: t for t in d["tasks"]}
        keys = rep["task_keys"]
        folded = rep.get("folded") or {}                       # --granularity product: steps folded into the asset they feed

        def eff_up(tid: str) -> list[str]:
            out: list[str] = []
            for u in tasks[tid]["upstream"]:
                out += eff_up(u) if (tasks[u]["type"] in ELIDED and u not in keys) or u in folded else [u]      # in the graph form an EmptyOperator is a step; a folded step has no asset
            return list(dict.fromkeys(out))

        dag_unit = by_dag.get(dag_id)
        if dag_unit is not None:
            tgt: dict = {"jobs": [safe(dag_id)]} if not (rep.get("output") == "components" and not rep.get("schedule")) else {"none": "the DAG has no schedule, so the components define no job"}
            if rep.get("schedule"):
                tgt["schedules"] = [{"name": f"{safe(dag_id)}_schedule", "cron": rep["schedule"]}]
            dag_unit["target"] = tgt
            stats["dags_planned"] += 1
        caveat = {c["task"]: c["reason"] for c in rep.get("caveats") or [] if c.get("kind", "stub") == "stub"}
        for tid, t in tasks.items():
            u = by_task.get((dag_id, tid))
            if u is None:
                continue
            if tid in caveat:                                  # the asset exists in the DAG's shape, but its body is a marked stub: the unit is not translated
                if u["status"] == "pending":
                    status.defer(manifest, u["id"], f"AIRFLOW-CAVEAT stub (the DAG converts around it): {caveat[tid]}"[:900])
                    stats["tasks_deferred"] += 1
                continue
            if tid in folded:                                   # no asset of its own: it runs inside its consumer's asset
                u["target"] = {"assets": [keys[tid]], "folded_into": folded[tid]}
            elif tid in (rep.get("cosmos_tasks") or {}):          # a Cosmos task: it became part of a dbt component, whose assets are this unit's target
                u["target"] = {"assets": rep["cosmos_tasks"][tid], "deps": {}}
            elif t["type"] in ELIDED and tid not in keys:         # in the graph form an EmptyOperator is a real step
                u["target"] = {"none": "EmptyOperator elided; its dependencies were rewired through it"}
            else:
                key = keys[tid]
                ups = {keys[x] for x in eff_up(tid) if x in keys} | {k for x in eff_up(tid) for k in (rep.get("cosmos_sinks") or {}).get(x, [])}
                deps = ups | {i_key(i) for i in t["inlets"] if i_key(i) != key}
                if not eff_up(tid):                                    # root tasks also depend on the assets that trigger the DAG
                    deps |= {i_key(x) for x in rep.get("triggers", [])}
                u["target"] = {"assets": [key], "deps": {key: sorted(deps)}}
            stats["tasks_planned"] += 1
    stats["left_pending"] = sum(1 for u in units if u["status"] == "pending" and not u.get("target"))
    return stats


def i_key(ref: str) -> str:
    return "/".join(convert_ir.key_from_asset(ref))             # the converter's own function, in the mode the report was written with


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("manifest", type=Path)
    ap.add_argument("ir", type=Path)
    ap.add_argument("report", type=Path)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    m = json.loads(a.manifest.read_text())
    stats = apply(m, json.loads(a.ir.read_text()), json.loads(a.report.read_text()))
    (a.out or a.manifest).write_text(json.dumps(m, indent=2, default=str) + "\n")
    print(", ".join(f"{k}={v}" for k, v in stats.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
