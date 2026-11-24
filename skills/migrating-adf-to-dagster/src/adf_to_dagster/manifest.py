"""Migration manifest: one record per source object, with a disposition state machine.

The converter enumerates (deterministic completeness); the agent or a human resolves
anything the converter could not translate. A migration is only done when every unit is
`complete` or `deferred` with a reason (`status summary` exits nonzero otherwise).
"""
from __future__ import annotations

import json
from pathlib import Path

from .convert import CONVERTED, REVIEW, Result

# MECH: converter output is expected to be right as generated; JUDG: a human/agent must confirm
# or fix; NONE: no automatic translation exists.
CLASSIFICATION = {CONVERTED: "MECH", REVIEW: "JUDG"}


def build_manifest(result: Result) -> dict:
    units = []
    for n, item in enumerate(result.items, 1):
        units.append({
            "id": f"U{n:03d}",
            "kind": item.kind,
            "source": item.source,
            "artifact": item.artifact,
            "classification": CLASSIFICATION.get(item.status, "NONE"),
            "caveat": item.caveat,
            "status": "pending",          # pending | complete | deferred
            "evidence": "",
            "reason": "",
            "notes": item.notes,
            "expected": item.expected,
        })
    return {"version": 1, "units": units, "questions": result.questions}


def save(manifest: dict, path: Path) -> None:
    Path(path).write_text(json.dumps(manifest, indent=2) + "\n")


def load(path: Path) -> dict:
    return json.loads(Path(path).read_text())


def _unit(manifest: dict, unit_id: str) -> dict:
    for u in manifest["units"]:
        if u["id"] == unit_id:
            return u
    raise KeyError(f"no unit {unit_id!r}")


def advance(manifest: dict, unit_id: str, evidence: str) -> None:
    if not evidence.strip():
        raise ValueError("evidence is required to mark a unit complete")
    u = _unit(manifest, unit_id)
    u.update(status="complete", evidence=evidence, reason="")


def defer(manifest: dict, unit_id: str, reason: str) -> None:
    if not reason.strip():
        raise ValueError("a reason is required to defer a unit")
    u = _unit(manifest, unit_id)
    u.update(status="deferred", reason=reason)


def reopen(manifest: dict, unit_id: str, reason: str) -> None:
    u = _unit(manifest, unit_id)
    u.update(status="pending", evidence="", reason=reason)


def summary(manifest: dict) -> tuple[str, bool]:
    counts = {"complete": 0, "deferred": 0, "pending": 0}
    for u in manifest["units"]:
        counts[u["status"]] += 1
    lines = [f"{len(manifest['units'])} units: {counts['complete']} complete, "
             f"{counts['deferred']} deferred, {counts['pending']} pending"]
    for u in manifest["units"]:
        if u["status"] == "pending":
            lines.append(f"  PENDING {u['id']} [{u['classification']}{'/' + u['caveat'] if u.get('caveat') else ''}] {u['kind']}: {u['source']}")
    return "\n".join(lines), counts["pending"] == 0


def render_report(manifest: dict) -> str:
    """MIGRATION.md with dispositions, from the manifest (the source of truth)."""
    text, ok = summary(manifest)
    lines = ["# Migration report", "", text.splitlines()[0], "",
             "A unit marked complete has passed the gates listed in its evidence. It has not been compared against "
             "ADF run results; run both in parallel before cutover.", "",
             "| Unit | Kind | Source | Dagster artifact | Class | Status | Notes / evidence / reason |",
             "|---|---|---|---|---|---|---|"]
    for u in manifest["units"]:
        detail = "; ".join(filter(None, [*u["notes"], u["evidence"] and f"evidence: {u['evidence']}",
                                         u["reason"] and f"reason: {u['reason']}"]))
        cls = u["classification"] + (f" ({u['caveat']})" if u.get("caveat") else "")
        lines.append(f"| {u['id']} | {u['kind']} | {u['source']} | {u['artifact']} | {cls} | "
                     f"{u['status']} | {detail.replace('|', '/')} |")
    from .questions import render_questions
    lines += render_questions(manifest.get("questions") or {})
    return "\n".join(lines) + "\n"
