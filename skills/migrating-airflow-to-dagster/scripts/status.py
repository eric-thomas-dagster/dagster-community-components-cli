#!/usr/bin/env python3
"""Per-unit state machine for a migration manifest (pending -> complete | deferred).

    python3 status.py summary manifest.json            # exits nonzero while any unit is pending
    python3 status.py list manifest.json [--pending]
    python3 status.py advance manifest.json A003 --evidence "dg check defs passes; parity row added"
    python3 status.py defer manifest.json A007 --reason "needs a custom timetable; tracked separately"
    python3 status.py reopen manifest.json A003 --reason "parity check failed"

Only this script (or its functions) should change a unit's `status`; plan fields (`target`) are the
planner's to write.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _unit(m: dict, unit_id: str) -> dict:
    for u in m["units"]:
        if u["id"] == unit_id:
            return u
    raise KeyError(f"no unit {unit_id!r}")


def advance(m: dict, unit_id: str, evidence: str) -> None:
    if not evidence.strip():
        raise ValueError("evidence is required to mark a unit complete")
    _unit(m, unit_id).update(status="complete", evidence=evidence, reason="")


def defer(m: dict, unit_id: str, reason: str) -> None:
    if not reason.strip():
        raise ValueError("a reason is required to defer a unit")
    _unit(m, unit_id).update(status="deferred", reason=reason)


def reopen(m: dict, unit_id: str, reason: str) -> None:
    _unit(m, unit_id).update(status="pending", evidence="", reason=reason)


def summary(m: dict) -> tuple[str, bool]:
    counts = {"complete": 0, "deferred": 0, "pending": 0}
    for u in m["units"]:
        counts[u["status"]] += 1
    lines = [f"{len(m['units'])} units: {counts['complete']} complete, {counts['deferred']} deferred, {counts['pending']} pending"]
    for u in m["units"]:
        if u["status"] == "pending":
            name = u.get("task_id") or u.get("dag_id")
            lines.append(f"  PENDING {u['id']} [{u['classification']}] {u['kind']}: {u.get('dag_id', '')}/{name}" if u["kind"] == "task"
                         else f"  PENDING {u['id']} [{u['classification']}] {u['kind']}: {name}")
    return "\n".join(lines), counts["pending"] == 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["summary", "list", "advance", "defer", "reopen"])
    ap.add_argument("manifest", type=Path)
    ap.add_argument("unit", nargs="?")
    ap.add_argument("--evidence", default="")
    ap.add_argument("--reason", default="")
    ap.add_argument("--pending", action="store_true")
    a = ap.parse_args(argv)
    m = json.loads(a.manifest.read_text())
    if a.cmd == "summary":
        text, ok = summary(m)
        print(text)
        return 0 if ok else 1
    if a.cmd == "list":
        for u in m["units"]:
            if not a.pending or u["status"] == "pending":
                print(f"{u['id']} {u['status']:9} {u['classification']:8} {u['kind']:11} {u.get('dag_id', '')}/{u.get('task_id', '')}")
        return 0
    if not a.unit:
        ap.error(f"{a.cmd} needs a unit id")
    {"advance": lambda: advance(m, a.unit, a.evidence), "defer": lambda: defer(m, a.unit, a.reason),
     "reopen": lambda: reopen(m, a.unit, a.reason)}[a.cmd]()
    a.manifest.write_text(json.dumps(m, indent=2) + "\n")
    print(f"{a.unit} {a.cmd}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
