#!/usr/bin/env python3
"""Plan the migration in waves: a pattern, a complexity bucket, a path, a wave and a coarse effort per DAG, plus a suggested pilot set.

    python3 plan.py manifest.json [--ir ir.json] [--report conversion_report.json] [--out-dir .]

Writes `plan.md` (for people) and `plan.json` (for tools). Read-only on every input. Deterministic: the same inputs give byte-identical outputs
(no timestamps, no randomness, every list sorted). EVERYTHING IN THE PLAN IS A HEURISTIC: the rules and the formula are printed at the top of
`plan.md` so they can be argued with and changed (`RULES` below); they are not a quote and not a measurement of your team.

Inputs: the manifest (`inventory.py`, optionally reconciled) is required. With `--ir` (from `airflow_ir.py`) the pattern uses the operator types
Airflow really built, which matters for factory-generated DAGs the static scan cannot see. With `--report` (from `convert_ir.py`) the plan uses what
the converter did: a DAG it converted without a stubbed task needs review, not a rewrite, and a deferred DAG says why.

Complexity uses this skill's own classification (`reference/mapping.md`, `reference/conversion.md`): MECH (mechanical), JUDG (needs care: a judgment
call or confirmation), REDESIGN (needs a design decision). No other taxonomy is introduced.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

BUCKET = {"MECH": "mechanical", "JUDG": "needs-care", "REDESIGN": "redesign"}
ORDER = {"MECH": 0, "JUDG": 1, "REDESIGN": 2}

# The heuristics, in one place. Changing a number here changes the plan; the numbers are printed in plan.md.
RULES = {
    "task_points": {"MECH": 1, "JUDG": 3, "REDESIGN": 8},
    "signal_points": 2,                       # per distinct signal present in the DAG (XCom, dataset, dynamic mapping, Jinja, connections, pools, ...)
    "effort_days": {"base": {"mechanical": 0.5, "needs-care": 2.0, "redesign": 5.0}, "per_task": {"MECH": 0.1, "JUDG": 0.4, "REDESIGN": 1.0}, "per_stub": 0.5,
                    "reviewed_only_factor": 0.5},
    "size_days": [(0.5, "XS"), (1.5, "S"), (3.0, "M"), (6.0, "L")],       # above the last: XL
    "pilot_size": 3,
}
HIGH_LINEAGE = {"dbt", "elt_transfer", "sql"}
MEDIUM_LINEAGE = {"python_etl", "containers_spark", "sensor_driven"}
PATTERNS = [   # first match wins; the share is the fraction of the DAG's tasks whose operator matches
    ("dbt", re.compile(r"dbt|cosmos", re.I), 0.5),
    ("containers_spark", re.compile(r"kubernetespod|docker|spark|databricks|emr|dataproc|ecs|batch|beam|glue|dataflow", re.I), 0.4),
    ("elt_transfer", re.compile(r"airbyte|fivetran|transfer|s3to|tos3|gcsto|tos3|copyfrom|copyinto|s3copy|s3list|s3delete|localfilesystemtos3|stitch|meltano|dlt", re.I), 0.4),
    ("sql", re.compile(r"sql|postgres|mysql|mssql|snowflake|bigquery|redshift|trino|presto|athena|sqlite", re.I), 0.5),
    ("sensor_driven", re.compile(r"sensor", re.I), 0.34),
    ("ops_notification", re.compile(r"email|slack|pagerduty|opsgenie|teams|notif|latestonly|empty|dummy|ssh", re.I), 0.6),
]
SENSOR_DAG_SCHEDULES = {"dataset", "asset"}
DAG_KINDS = {"dag", "cosmos", "dag_factory", "blueprint_dag"}


def load(p: Path | None) -> Any:
    return json.loads(p.read_text()) if p else None


def operators_of(dag_id: str, tasks: list[dict[str, Any]], ir_dag: dict[str, Any] | None) -> list[str]:
    if ir_dag:
        return [t["type"] for t in ir_dag["tasks"]]
    return [t.get("operator") or "" for t in tasks]


def pattern_of(ops: list[str], schedule_kind: str | None) -> tuple[str, str]:
    """(pattern, why). An operator counts for the first pattern it matches; a pattern wins when its share of the tasks reaches its threshold."""
    n = len(ops) or 1
    per_task = []
    for o in ops:
        hit = next((name for name, rx, _ in PATTERNS if rx.search(o)), None)
        per_task.append(hit)
    counts = Counter(h for h in per_task if h)
    for name, _, share in PATTERNS:
        if counts.get(name, 0) / n >= share:
            return name, f"{counts[name]} of {len(ops)} tasks are {name.replace('_', ' ')} operators"
    if schedule_kind in SENSOR_DAG_SCHEDULES:
        return "sensor_driven", f"scheduled on {schedule_kind}s"
    py = sum(1 for o in ops if re.search(r"python|^@task|bash|taskflow|decorated|virtualenv|externalpython", o, re.I))
    if py or not ops:
        return "python_etl", f"{py} of {len(ops)} tasks are Python or Bash"
    return "python_etl", "no operator matched a more specific pattern"


def size_of(days: float) -> str:
    return next((label for limit, label in RULES["size_days"] if days <= limit), "XL")


def half(x: float) -> float:
    return round(x * 2) / 2 if x else 0.0


def plan_dag(unit: dict[str, Any], tasks: list[dict[str, Any]], ir_dag: dict[str, Any] | None, rep: dict[str, Any] | None) -> dict[str, Any]:
    classes = Counter({"MECH": 0, "JUDG": 0, "REDESIGN": 0})
    for t in tasks:
        classes[t.get("classification") or "JUDG"] += 1
    if not tasks:
        classes.update(unit.get("task_classes") or {})
    klass = max([unit.get("classification") or "MECH", *[c for c, n in classes.items() if n]], key=lambda c: ORDER[c])      # the worst of the DAG's own class and its tasks
    signals = sorted({k for t in [unit, *tasks] for k in (t.get("signals") or {})})
    ops = operators_of(unit["dag_id"], tasks, ir_dag)
    sched = (unit.get("schedule") or {}).get("kind")
    pattern, why_pattern = pattern_of(ops, sched)
    cosmos = unit["kind"] == "cosmos" or "cosmos" in signals or bool(ir_dag and any(t.get("cosmos") for t in ir_dag["tasks"]))
    if cosmos:
        pattern, why_pattern = "dbt", "Cosmos DbtDag / DbtTaskGroup / dbt operators"
    pts = sum(RULES["task_points"][c] * n for c, n in classes.items()) + RULES["signal_points"] * len(signals)
    bucket = BUCKET[klass]
    converted = rep is not None and rep.get("status") == "converted"
    stubs = [c for c in (rep or {}).get("caveats", []) if c.get("kind") == "stub"] if rep else []
    degraded = [c for c in (rep or {}).get("caveats", []) if c.get("kind") == "degraded"] if rep else []
    effective = bucket
    reasons = [f"{bucket} from the manifest ({unit.get('classification_reason') or 'no reason recorded'})"]
    if converted and not stubs:
        effective = "needs-care" if bucket == "redesign" else "mechanical"
        reasons.append("the converter produced Dagster code with no stubbed task, so what remains is review and parity" + (f" ({len(degraded)} stated difference(s) to confirm)" if degraded else ""))
    elif converted and stubs:
        reasons.append(f"the converter converted the shape but stubbed {len(stubs)} task(s): {', '.join(sorted({c['task'] for c in stubs})[:5])}")
    elif rep is not None and rep.get("status") == "deferred":
        reasons.append("the converter deferred the DAG: " + str(rep.get("reason"))[:160])
    eff = RULES["effort_days"]
    days = eff["base"][effective] + sum(eff["per_task"][c] * n for c, n in classes.items()) + eff["per_stub"] * len(stubs)
    if converted and not stubs:
        days *= eff["reviewed_only_factor"]
    days = max(half(days), 0.5)
    lineage = "high" if pattern in HIGH_LINEAGE else "medium" if pattern in MEDIUM_LINEAGE else "low"
    scheduled = bool(sched) and sched not in ("none", "manual", None)
    if effective == "mechanical":
        path, why = "native rewrite", "mechanical: the converter or a short manual pass covers it"
    elif effective == "needs-care" and not (rep is not None and rep.get("status") == "deferred"):
        path, why = "native rewrite", "needs care (judgment calls, each recorded) but has a mapping"
    elif scheduled or lineage == "high":
        path, why = "observe/bridge with airlift", "needs a design decision: observe it (and proxy it when ready) while the redesign is done"
    else:
        path, why = "defer", "needs a design decision and has no schedule or lineage value that justifies doing it first"
    return {"dag_id": unit["dag_id"], "unit": unit["id"], "file": unit.get("file"), "schedule": sched, "tasks": len(ir_dag["tasks"]) if ir_dag else len(tasks),
            "pattern": pattern, "pattern_why": why_pattern, "classes": {k: classes[k] for k in ("MECH", "JUDG", "REDESIGN")}, "signals": signals, "score": pts,
            "bucket": bucket, "effective_bucket": effective, "bucket_reasons": reasons, "lineage_value": lineage, "path": path, "path_why": why, "effort_days": days,
            "size": size_of(days), "stubs": len(stubs), "status": unit.get("status"), "conversion": (rep or {}).get("status")}


WAVE_NAMES = {1: "mechanical, high lineage value (dbt, ELT, SQL)", 2: "other mechanical, and needs-care with medium or high lineage value", 3: "remaining needs-care",
              4: "redesign-heavy, deferred and bridged"}


def wave_of(r: dict[str, Any]) -> int:
    """Wave 1: mechanical with high lineage value. 2: the other mechanical DAGs, and needs-care ones with medium or high lineage value. 3: the remaining
    needs-care ones. 4: redesign, and anything whose path is not a native rewrite. The numbers are fixed (a wave with no DAG is simply not listed), so
    wave 1 always means low complexity with high lineage value and the last wave always means redesign-heavy."""
    if r["effective_bucket"] == "redesign" or r["path"] != "native rewrite":
        return 4
    if r["effective_bucket"] == "mechanical" and r["lineage_value"] == "high":
        return 1
    if r["effective_bucket"] == "mechanical" or r["lineage_value"] in ("high", "medium"):
        return 2
    return 3


def pilots(rows: list[dict[str, Any]], n: int) -> list[dict[str, str]]:
    """The skill's trial (SKILL.md, Phase 3): one plain scheduled DAG, one with a sensor or dataset trigger, one with XCom, branching or mapping. Lowest score first."""
    chosen: list[dict[str, str]] = []
    key = lambda r: (r["score"], r["dag_id"])           # noqa: E731
    cand = [r for r in rows if r["path"] == "native rewrite"]
    kinds = [("a plain scheduled DAG", lambda r: r["schedule"] not in (None, "none", "manual") and not ({"dataset_or_asset", "xcom", "dynamic_task_mapping"} & set(r["signals"])) and r["pattern"] != "sensor_driven"),
             ("a sensor or dataset trigger", lambda r: "dataset_or_asset" in r["signals"] or r["pattern"] == "sensor_driven"),
             ("XCom, branching or dynamic mapping", lambda r: bool({"xcom", "dynamic_task_mapping", "branch"} & set(r["signals"])))]
    for label, test in kinds:
        pick = next((r for r in sorted(cand, key=key) if test(r) and r["dag_id"] not in {c["dag_id"] for c in chosen}), None)
        if pick:
            chosen.append({"dag_id": pick["dag_id"], "covers": label, "wave": pick["wave"]})
    for r in sorted(cand, key=key):
        if len(chosen) >= n:
            break
        if r["dag_id"] not in {c["dag_id"] for c in chosen}:
            chosen.append({"dag_id": r["dag_id"], "covers": "next lowest complexity", "wave": r["wave"]})
    return chosen[:n]


def build(manifest: dict[str, Any], ir: dict[str, Any] | None, report: dict[str, Any] | None) -> dict[str, Any]:
    units = manifest["units"]
    tasks_by: dict[str, list[dict[str, Any]]] = {}
    for u in units:
        if u["kind"] == "task":
            tasks_by.setdefault(u["dag_id"], []).append(u)
    ir_dags = {d["dag_id"]: d for d in (ir or {}).get("dags", [])}
    reps = (report or {}).get("dags", {})
    rows = [plan_dag(u, sorted(tasks_by.get(u["dag_id"], []), key=lambda t: t["id"]), ir_dags.get(u["dag_id"]), reps.get(u["dag_id"]))
            for u in sorted((u for u in units if u["kind"] in DAG_KINDS and not u.get("factory")), key=lambda u: u["dag_id"])]
    seen: set[str] = set()
    rows = [r for r in rows if not (r["dag_id"] in seen or seen.add(r["dag_id"]))]
    for r in rows:
        r["wave"] = wave_of(r)
        r["route"], r["route_why"] = None, None
    rows.sort(key=lambda r: (r["wave"], r["score"], r["dag_id"]))
    waves: dict[int, dict[str, Any]] = {}
    for r in rows:
        w = waves.setdefault(r["wave"], {"wave": r["wave"], "dags": [], "effort_days": 0.0})
        w["dags"].append(r["dag_id"])
        w["effort_days"] += r["effort_days"]
    pilot = pilots(rows, RULES["pilot_size"])
    pilot_ids = {c["dag_id"] for c in pilot}
    for r in rows:                                                      # the route inside tier 3 (how the code is written): convert (the converter) or rewrite (spec and prompt)
        if r["path"] != "native rewrite":
            r["route"], r["route_why"] = "n/a", "not a tier 3 (native) DAG yet: " + r["path"]
        elif r["stubs"] or r["conversion"] == "deferred":
            r["route"], r["route_why"] = "rewrite", "the converter stubbed or deferred part of it"
        elif r["effective_bucket"] == "redesign" or r["wave"] == 4:
            r["route"], r["route_why"] = "rewrite", "redesign-heavy (last wave)"
        elif r["dag_id"] in pilot_ids:
            r["route"], r["route_why"] = "rewrite", "pilot: idiomatic output matters while the mapping is being learned (the converter output is the reference draft and fallback)"
        else:
            r["route"], r["route_why"] = "convert", "mechanical bulk: the converter's output, gated"
    return {"heuristic": True, "rules": {k: (v if k != "size_days" else [list(x) for x in v]) for k, v in RULES.items()},
            "inputs": {"manifest": True, "ir": ir is not None, "report": report is not None}, "dags": rows, "waves": [waves[k] for k in sorted(waves)],
            "pilot": pilot, "by_route": dict(sorted(Counter(r["route"] for r in rows).items())), "total_effort_days": sum(r["effort_days"] for r in rows),
            "by_pattern": dict(sorted(Counter(r["pattern"] for r in rows).items())), "by_path": dict(sorted(Counter(r["path"] for r in rows).items()))}


def render(p: dict[str, Any]) -> str:
    r = p["rules"]
    out = ["# Migration plan", "",
           "**Every number and label here is a heuristic.** It is computed from the manifest" + (", the IR" if p["inputs"]["ir"] else "") + (" and the conversion report" if p["inputs"]["report"] else "")
           + " by fixed rules (below), not measured on your team. Use it to order the work and to start the conversation about scope; replace the estimates with your own after the pilot.", "",
           f"{len(p['dags'])} DAGs, {len(p['waves'])} waves, about {p['total_effort_days']:g} person-days in total (a coarse estimate; see the formula).", "",
           "## Suggested pilot", "", "Migrate these first, end to end through the gates (`SKILL.md`, Phase 3); what they teach updates the mapping before the rest.", ""]
    for c in p["pilot"]:
        out.append(f"- `{c['dag_id']}` (wave {c['wave']}): {c['covers']}")
    if not p["pilot"]:
        out.append("- none: no DAG qualifies for a native rewrite")
    out += ["", "## Waves", ""]
    for w in p["waves"]:
        out += [f"### Wave {w['wave']}: {WAVE_NAMES[w['wave']]}", "", f"{len(w['dags'])} DAGs, about {w['effort_days']:g} person-days.", "",
                "| DAG | Pattern | Complexity | Path | Route | Effort | Score | Why |", "|---|---|---|---|---|---|---|---|"]
        for d in p["dags"]:
            if d["wave"] == w["wave"]:
                cx = d["bucket"] if d["bucket"] == d["effective_bucket"] else f"{d['bucket']} (converter: {d['effective_bucket']})"
                out.append(f"| `{d['dag_id']}` | {d['pattern'].replace('_', ' ')} | {cx} | {d['path']} | {d['route']} | {d['size']} ({d['effort_days']:g} d) | {d['score']} | {d['path_why']}; {d['pattern_why']} |")
        out.append("")
    out += ["## How this was computed", "",
            "**Pattern** (first match wins, by the share of the DAG's tasks whose operator matches): dbt or Cosmos, containers/Spark, ELT/transfer, SQL, sensor-driven, ops/notification; otherwise Python ETL.", "",
            f"**Complexity** is the skill's own classification: MECH = mechanical, JUDG = needs care, REDESIGN = redesign (`reference/mapping.md`). The DAG's bucket is the worst class among the DAG's own classification and its tasks'. "
            "With a conversion report, a converted DAG with no stubbed task is treated as mechanical (review and parity remain), or as needs-care when it was redesign.", "",
            f"**Score** = {r['task_points']['MECH']} x MECH tasks + {r['task_points']['JUDG']} x JUDG tasks + {r['task_points']['REDESIGN']} x REDESIGN tasks + {r['signal_points']} x distinct signals "
            "(XCom, datasets, dynamic mapping, Jinja, connections, pools, task groups, ...). Used to order DAGs within a wave and to pick the pilot; it is not a size.", "",
            f"**Effort** (person-days, rounded to half days, minimum 0.5) = base ({', '.join(f'{k} {v:g}' for k, v in r['effort_days']['base'].items())}) by the effective bucket + "
            f"{r['effort_days']['per_task']['MECH']:g} x MECH + {r['effort_days']['per_task']['JUDG']:g} x JUDG + {r['effort_days']['per_task']['REDESIGN']:g} x REDESIGN tasks + {r['effort_days']['per_stub']:g} per stubbed task; "
            f"multiplied by {r['effort_days']['reviewed_only_factor']:g} when the converter produced the DAG with no stub. Size: " + ", ".join(f"{lab} up to {lim:g} d" for lim, lab in r["size_days"]) + ", else XL. "
            "It covers conversion, review and a parity check; it does not cover cutover coordination, infrastructure, training or on-call changes.", "",
            "**Path.** Native rewrite: mechanical, or needs-care with a mapping. Observe/bridge with airlift (`reference/tiers.md`, `reference/airlift.md`): redesign DAGs that are scheduled or have high lineage value, so Dagster sees them while the redesign is done. "
            "Defer: redesign with no schedule and no lineage value.", "",
            "**Wave** (fixed numbers; a wave with no DAG is not listed). " + " ".join(f"{n}: {t}." for n, t in WAVE_NAMES.items()) + " Lineage value is high for dbt, ELT/transfer and SQL DAGs, medium for Python ETL, containers/Spark and sensor-driven ones, low for ops/notification.", "",
            "**Route** (how the tier 3 code gets written; see `SKILL.md`, \"When to use the rewrite route\"): `rewrite` (the generated spec and prompt, `scripts/dag_spec.py` and `rewrite_prompt.py`) for DAGs the converter stubbed or deferred, redesign-heavy DAGs and the last wave, and the pilot DAGs; `convert` (the deterministic converter) for the mechanical bulk; `n/a` where the path is not yet native. The rewrite route spends model effort on every DAG it is used for, and the effort estimate above does not include it. The converter's output is the reference draft and the fallback for every rewrite.", "",
            "**Pilot.** The lowest-score native-rewrite DAG for each of: a plain scheduled DAG, a sensor or dataset trigger, XCom/branching/mapping; then the next lowest until there are three.", "",
            "## Totals", "", "| Pattern | DAGs |", "|---|---|"] + [f"| {k.replace('_', ' ')} | {v} |" for k, v in p["by_pattern"].items()] + ["", "| Path | DAGs |", "|---|---|"] \
        + [f"| {k} | {v} |" for k, v in p["by_path"].items()] + ["", "| Route | DAGs |", "|---|---|"] + [f"| {k} | {v} |" for k, v in p["by_route"].items()] + [""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("manifest", type=Path)
    ap.add_argument("--ir", type=Path)
    ap.add_argument("--report", type=Path)
    ap.add_argument("--out-dir", type=Path, default=Path("."))
    a = ap.parse_args(argv)
    plan = build(load(a.manifest), load(a.ir), load(a.report))
    a.out_dir.mkdir(parents=True, exist_ok=True)
    (a.out_dir / "plan.json").write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    (a.out_dir / "plan.md").write_text(render(plan))
    print(f"plan: {len(plan['dags'])} DAGs in {len(plan['waves'])} waves, ~{plan['total_effort_days']:g} person-days (heuristic); pilot: {', '.join(c['dag_id'] for c in plan['pilot']) or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
