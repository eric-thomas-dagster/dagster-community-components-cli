#!/usr/bin/env python3
"""Regression guard for the converter: a snapshot of every measured DAG's verdict, and a check that a new measurement did not get worse.

    python3 verdict_snapshot.py save  WORKDIR [WORKDIR ...] [--conv-dir DIR ...] [--route converter] [--out reference/verdict-snapshot.json]
    python3 verdict_snapshot.py check WORKDIR [WORKDIR ...] [--conv-dir DIR ...] [--route converter] [--snapshot reference/verdict-snapshot.json]

A WORKDIR is a work directory of `eval_behavior.py` (`<work>/<run>-private/behavior/<dag>/behavior.json`). `save` reads the FRESHEST `behavior.json` per DAG key across all the work directories given
(newest file modification time wins), and writes one entry per key: the route's verdict (`stub-matches-failure` is its own verdict), the harness's reason, `thin` or `substantive` (with what was not observed when thin),
and a digest of the converter's report for that DAG (stubbed tasks, degraded tasks, caveat count, whether it was deferred) read from the `--conv-dir`s (`<conv>/<repo>.<airflow>.ra[d]/report.json`).

`check` compares a measurement run (the same kind of work directories) with the snapshot and prints
    regressions   a verdict that got worse (the order is equivalent > equivalent-with-differences > matches-baseline-failure > stub-matches-failure > different = not-comparable > no-output; where the baseline fails BY DESIGN, matches-baseline-failure is the top result and stub-matches-failure the lowest of the failure-matching ones), an `equivalent` that turned
                  from substantive to thin (less was observed), a new stub / degraded task / caveat in the converter's report
    improvements  the reverse
    changed       a DAG whose baseline became or stopped being `baseline-failed` (an environment change: not ranked)
    not measured  snapshot DAGs the run did not cover (listed, never a regression: run the whole set to be sure)
and the unchanged counts. The exit status is 1 when there is any regression, else 0. Nothing here runs Airflow or the converter.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DEFAULT_SNAPSHOT = HERE.parent / "reference" / "verdict-snapshot.json"
RANK = {"equivalent": 6, "equivalent-with-differences": 5, "matches-baseline-failure": 4, "stub-matches-failure": 3, "different": 2, "not-comparable": 2, "no-output": 1}
UNRANKED = "baseline-failed"
BY_DESIGN = "by-design-failure"                                              # the baseline class of a DAG whose own code fails in Airflow


def rank(verdict: str | None, baseline: str | None = None) -> int:
    """The order of verdicts. When the baseline FAILS BY DESIGN, failing the same way is the correct and top result (`matches-baseline-failure` outranks everything, including equivalent-with-differences, which cannot
    be reached by a rewrite that fails where the original fails); `stub-matches-failure` (a stub that merely happens to fail) stays the lowest of the failure-matching verdicts."""
    if verdict == "matches-baseline-failure" and baseline == BY_DESIGN:
        return max(RANK.values()) + 1
    return RANK.get(verdict or "", 0)
OBSERVABLE = ("values", "files", "sqlite", "http", "smtp", "s3", "stdout", "failure")


def behavior_files(work: Path) -> list[Path]:
    return sorted(Path(work).glob("*-private/behavior/*/behavior.json"))


def digest_of(rep: dict[str, Any] | None) -> dict[str, Any] | None:
    """what the converter's report says about a DAG that a verdict does not show: stubbed and degraded tasks, the caveat count, deferral."""
    if rep is None:
        return None
    cav = rep.get("caveats") or []
    return {"deferred": rep.get("status") != "converted", "stubs": sorted({c.get("task") or "(dag)" for c in cav if c.get("kind") == "stub"}),
            "degraded": sorted({c.get("task") or "(dag)" for c in cav if c.get("kind") == "degraded"}), "caveats": len(cav)}


def report_index(conv_dirs: list[Path]) -> dict[tuple[str, str], dict[str, Any]]:
    """(repo, dag_id) -> the converter's report entry, from `<conv>/<repo>.<airflow>.ra[d]/report.json`; where several directories hold one, the newest report wins."""
    found: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
    for d in conv_dirs:
        for rp in sorted(Path(d).glob("*/report.json")):
            repo = rp.parent.name.rsplit(".", 2)[0]
            try:
                dags = json.loads(rp.read_text()).get("dags", {})
            except (OSError, ValueError):
                continue
            for dag_id, rep in dags.items():
                if (repo, dag_id) not in found or rp.stat().st_mtime > found[(repo, dag_id)][0]:
                    found[(repo, dag_id)] = (rp.stat().st_mtime, rep)
    return {k: v[1] for k, v in found.items()}


def collect(work_dirs: list[Path], conv_dirs: list[Path] | None = None, route: str = "converter") -> dict[str, dict[str, Any]]:
    """key -> entry, from the freshest behavior.json per DAG key."""
    fresh: dict[str, tuple[float, dict[str, Any]]] = {}
    for w in work_dirs:
        for f in behavior_files(w):
            try:
                d = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if not d.get("key") or route not in (d.get("routes") or {}):
                continue
            m = f.stat().st_mtime
            if d["key"] not in fresh or m > fresh[d["key"]][0]:
                fresh[d["key"]] = (m, d)
    index = report_index(conv_dirs or [])
    out: dict[str, dict[str, Any]] = {}
    for key, (_, d) in sorted(fresh.items()):
        x = d["routes"][route]
        ru = x.get("rollup") or {}
        thin = ru.get("thin")                                               # None: nothing ran or was compared (no-output, baseline-failed)
        dims = x.get("dimensions") or {}
        out[key] = {"verdict": ru.get("detail") or ru.get("verdict"), "detail": ru.get("why"), "thin": thin, "kind": "n/a" if thin is None else "thin" if thin else "substantive",
                    "unobserved": sorted(ru.get("unobserved") or [n for n in OBSERVABLE if dims.get(n, {}).get("verdict") == "not-compared"]) if thin else [],
                    "baseline": ((d.get("baseline") or {}).get("class") or [None])[0], "converter": digest_of(index.get((d.get("repo"), d.get("dag_id"))))}
    return out


def save(a: argparse.Namespace) -> int:
    cur = collect(a.workdirs, a.conv_dir, a.route)
    counts: dict[str, int] = {}
    for e in cur.values():
        counts[e["verdict"]] = counts.get(e["verdict"], 0) + 1
    snap = {"schema": 1, "route": a.route, "note": "regression guard: `verdict_snapshot.py check` compares a measurement run against this; regenerate with `save` after a deliberate change",
            "counts": {"dags": len(cur), "by_verdict": dict(sorted(counts.items())), "strict_thin": sum(1 for e in cur.values() if e["verdict"] == "equivalent" and e["thin"]),
                       "strict_substantive": sum(1 for e in cur.values() if e["verdict"] == "equivalent" and e["thin"] is False), "with_digest": sum(1 for e in cur.values() if e["converter"])},
            "dags": cur}
    Path(a.out).write_text(json.dumps(snap, indent=1, sort_keys=True) + "\n")
    c = snap["counts"]
    print(f"wrote {a.out}: {len(cur)} DAG(s); {c['by_verdict']}; strict {c['strict_substantive']} substantive + {c['strict_thin']} thin; converter digest for {c['with_digest']}")
    return 0


def compare(snap: dict[str, dict[str, Any]], cur: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """regressions / improvements / changed / not-measured / new / unchanged, each with the keys and the one-line reason."""
    res: dict[str, Any] = {"regressions": [], "improvements": [], "changed": [], "not_measured": sorted(set(snap) - set(cur)), "new": sorted(set(cur) - set(snap)), "unchanged": 0}
    for key in sorted(set(snap) & set(cur)):
        o, n = snap[key], cur[key]
        reg: list[str] = []
        imp: list[str] = []
        ov, nv = o["verdict"], n["verdict"]
        if (ov == UNRANKED) != (nv == UNRANKED):
            res["changed"].append({"key": key, "why": f"baseline: {ov} -> {nv}"})
            continue
        if ov != nv and ov != UNRANKED:
            ro, rn = rank(ov, o.get("baseline")), rank(nv, n.get("baseline"))
            if rn < ro:
                reg.append(f"verdict {ov} -> {nv}")
            elif rn > ro:
                imp.append(f"verdict {ov} -> {nv}")
        if ov == nv == "equivalent" and o.get("thin") != n.get("thin"):
            if n.get("thin"):
                reg.append("strict equivalent went from substantive to thin (less was observed)")
            else:
                imp.append("strict equivalent went from thin to substantive")
        od, nd = o.get("converter"), n.get("converter")
        if od and nd:
            for field, label in (("stubs", "stub"), ("degraded", "degraded task")):
                added, gone = sorted(set(nd[field]) - set(od[field])), sorted(set(od[field]) - set(nd[field]))
                if added:
                    reg.append(f"new {label}: {', '.join(added)}")
                if gone:
                    imp.append(f"{label} gone: {', '.join(gone)}")
            if nd["caveats"] > od["caveats"]:
                reg.append(f"caveats {od['caveats']} -> {nd['caveats']}")
            elif nd["caveats"] < od["caveats"]:
                imp.append(f"caveats {od['caveats']} -> {nd['caveats']}")
            if nd["deferred"] and not od["deferred"]:
                reg.append("the converter now defers this DAG")
            elif od["deferred"] and not nd["deferred"]:
                imp.append("the converter no longer defers this DAG")
        if reg:
            res["regressions"].append({"key": key, "why": "; ".join(reg), "from": ov, "to": nv})
        elif imp:
            res["improvements"].append({"key": key, "why": "; ".join(imp), "from": ov, "to": nv})
        else:
            res["unchanged"] += 1
    return res


def check(a: argparse.Namespace) -> int:
    snap = json.loads(Path(a.snapshot).read_text())
    cur = collect(a.workdirs, a.conv_dir, a.route)
    r = compare(snap["dags"], cur)
    print(f"snapshot {a.snapshot} ({len(snap['dags'])} DAGs) against {len(cur)} measured DAG(s)")
    for title, k in (("REGRESSIONS", "regressions"), ("improvements", "improvements"), ("changed (environment, not ranked)", "changed")):
        print(f"\n{title}: {len(r[k])}")
        for x in r[k]:
            print(f"  {x['key']}: {x['why']}")
    print(f"\nnot measured in this run: {len(r['not_measured'])}; new (not in the snapshot): {len(r['new'])}; unchanged: {r['unchanged']}")
    if r["regressions"]:
        print(f"\nFAIL: {len(r['regressions'])} regression(s)")
        return 1
    print("\nOK: no regressions")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for n in ("save", "check"):
        p = sub.add_parser(n)
        p.add_argument("workdirs", nargs="+", type=Path)
        p.add_argument("--conv-dir", nargs="*", type=Path, default=[], help="converter output directories (`<repo>.<airflow>.ra/report.json`) for the report digest")
        p.add_argument("--route", default="converter")
        if n == "save":
            p.add_argument("--out", type=Path, default=DEFAULT_SNAPSHOT)
        else:
            p.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    a = ap.parse_args(argv)
    return save(a) if a.cmd == "save" else check(a)


if __name__ == "__main__":
    sys.exit(main())
