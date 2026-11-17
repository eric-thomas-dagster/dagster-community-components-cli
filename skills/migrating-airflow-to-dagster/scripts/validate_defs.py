#!/usr/bin/env python3
"""Gate: do the Dagster definitions that actually load match the plan recorded in the manifest?

Each unit's `target` is written by the planner (Phase 2) and checked here:

    {"assets": ["a", "b"], "deps": {"b": ["a"]}, "jobs": ["nightly_etl"],
     "schedules": [{"name": "nightly_etl_schedule", "cron": "0 0 * * *"}]}
    {"none": "EmptyOperator; ordering kept through deps"}      # deliberately no artifact

Gate 1: `dg list defs --json` succeeds in the Dagster project (definitions load).
Gate 2: every target artifact exists and matches (asset deps, job names, schedule crons).
A unit with an empty `target` is reported as UNPLANNED, never silently skipped.

    python3 validate_defs.py <dagster_project> --manifest manifest.json [--advance]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import status  # noqa: E402


def load_defs(project: Path) -> dict:
    proc = subprocess.run(["uv", "run", "dg", "list", "defs", "--json"], cwd=project, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout)[-2000:])
    return json.loads(proc.stdout[proc.stdout.find("{"):])


def check(manifest: dict, defs: dict) -> tuple[list[str], list[str], list[str]]:
    """Return (passed unit ids, unplanned unit ids, problems)."""
    assets = {a["asset_key"]: set(a["dependency_keys"]) for a in defs["assets"]}
    jobs = {j["name"] for j in defs["jobs"]}
    scheds = {s["name"]: s["cron_schedule"] for s in defs["schedules"]}
    passed, unplanned, problems = [], [], []
    for u in manifest["units"]:
        t = u.get("target") or {}
        if not t:
            unplanned.append(u["id"])
            continue
        if "none" in t:
            if not str(t["none"]).strip():
                problems.append(f"{u['id']}: target 'none' needs a reason")
            else:
                passed.append(u["id"])
            continue
        bad: list[str] = []
        for a in t.get("assets", []):
            if a not in assets:
                bad.append(f"asset {a!r} missing")
        for a, deps in t.get("deps", {}).items():
            if a in assets and assets[a] != set(deps):
                bad.append(f"asset {a!r} deps {sorted(assets[a])} != planned {sorted(deps)}")
        for j in t.get("jobs", []):
            if j not in jobs:
                bad.append(f"job {j!r} missing")
        for s in t.get("schedules", []):
            if s["name"] not in scheds:
                bad.append(f"schedule {s['name']!r} missing")
            elif scheds[s["name"]] != s["cron"]:
                bad.append(f"schedule {s['name']!r} cron {scheds[s['name']]!r} != planned {s['cron']!r}")
        if bad:
            problems.extend(f"{u['id']}: {b}" for b in bad)
        else:
            passed.append(u["id"])
    return passed, unplanned, problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project", type=Path, help="Dagster project directory")
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--advance", action="store_true", help="mark passing MECH units complete")
    a = ap.parse_args(argv)
    manifest = json.loads(a.manifest.read_text())
    try:
        defs = load_defs(a.project)
    except RuntimeError as e:
        print(f"gate 1 FAIL: definitions did not load:\n{e}")
        return 1
    passed, unplanned, problems = check(manifest, defs)
    for p in problems:
        print(f"FAIL {p}")
    for uid in unplanned:
        print(f"UNPLANNED {uid}: no target recorded")
    print(f"gate 1 pass; gate 2: {len(passed)} units match, {len(problems)} problem(s), {len(unplanned)} unplanned")
    if a.advance and not problems:
        by_id = {u["id"]: u for u in manifest["units"]}
        n = 0
        for uid in passed:
            if by_id[uid]["classification"] == "MECH" and by_id[uid]["status"] == "pending":
                status.advance(manifest, uid, "gates 1-2 passed (definitions load and match the plan)")
                n += 1
        a.manifest.write_text(json.dumps(manifest, indent=2) + "\n")
        print(f"advanced {n} MECH unit(s)")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
