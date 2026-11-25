"""Compare the two routes' output for one unit by BEHAVIOR (graph order, schedules, pools, retries, commands and SQL, notification targets by name, stubs), not by code text.

Each side is measured against the same reference: the facts in the unit's spec (what RunMyJobs holds). Where the two sides agree with the spec they are equivalent on that dimension; where
one deviates, the report says which one matches the source facts. Where neither can be read by a machine (what replaced a precondition or a status handler) the report puts the two
statements side by side and says it did not compare them.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

from .accept import Ctx, _closure
from .introspect import command_present, corpus, introspect, package_of

NOT_ENFORCED = re.compile(r"not enforced|not applied|no automatic mapping|is ignored|not converted|not parsed", re.I)


def _converter_map(spec: dict) -> tuple[dict[str, list[str]], list[str]]:
    rows = spec["converter"]["rows"]
    by_src = {r["source"]: r for r in rows if r["kind"] == "process_definition"}
    call_assets: dict[str, list[str]] = {}
    for c in spec["calls"]:
        src = c["id"] if spec["kind"] == "process_definition" else f"{c['id'].split('/')[0]}/{c['call']} ({c['process_definition']})"
        r = by_src.get(src)
        if not r:
            continue
        a = r["expected"].get("asset")
        if not a:
            m = re.search(r"of asset (\S+)", r["artifact"])
            a = m.group(1) if m else None
        if a:
            call_assets[c["id"]] = [a]
    return call_assets, list(spec["converter"]["assets"])


def measure(spec: dict, snap: dict, call_assets: dict[str, list[str]], unit_assets: set[str], sched_filter, texts: list[str], sensor_calls: set[str] = frozenset()) -> dict[str, Any]:
    assets = {a["key"]: a for a in snap["assets"]}
    parents = {k: a["parents"] for k, a in assets.items()}
    ex = spec["expected"]
    lost = []
    sensor_pairs = 0
    for a, b in ex["order_pairs"]:
        ka, kb = call_assets.get(a, []), call_assets.get(b, [])
        if a in sensor_calls or b in sensor_calls:
            sensor_pairs += 1                              # replaced by a sensor that reacts to run status: not an asset dependency, not compared
        elif not ka or not kb:
            lost.append(f"{a} -> {b}: call not mapped to an asset")
        elif not (set(ka) & set(kb)) and not any(x in _closure(parents, y) for x in ka for y in kb):
            lost.append(f"{a} -> {b}")
    scheds = sorted((s["cron"], s["timezone"] or "UTC") for s in snap["schedules"] if sched_filter(s))
    pools = sorted({p for k in unit_assets if k in assets for p in assets[k]["pool"]})
    held: dict[str, int] = {}
    for cid, keys in call_assets.items():
        for k in keys:
            held[k] = max(held.get(k, 0), ex["retries"].get(cid, 0))
    bad_retry = [f"{k}: {assets[k]['retries']} != {r}" for k, r in sorted(held.items()) if r and k in assets and assets[k]["retries"] != r]
    cmd_missing = [c["call"] for c in ex["commands"] if not command_present(c, texts)]
    targets_missing = [t for t in ex["notification_targets"] if not any(t in x for x in texts)]
    return {"assets": sorted(k for k in unit_assets if k in assets), "order_lost": lost, "schedules": scheds, "pools": pools, "retry_mismatch": bad_retry,
            "sensor_pairs_not_compared": sensor_pairs, "commands_missing": cmd_missing, "notification_targets_missing": targets_missing, "checks": [c for c in snap["checks"] if c["asset"] in unit_assets]}


def converter_texts(project: Path, pkg: str, unit_assets: set[str]) -> list[str]:
    """What the converter wrote for this unit's assets: its component documents (the whole document, task-chain steps included), its generated scripts and the stub chunks of the
    fallback module (a stub keeps the source in comments). Documents of other units are left out, so a command cannot match another unit's."""
    import yaml

    from .introspect import norm_command
    root = project / "src" / pkg
    out: list[str] = []
    for y in sorted((root / "defs").rglob("defs.yaml")):
        for doc in y.read_text().split("\n---\n"):
            try:
                d = yaml.safe_load(doc) or {}
            except yaml.YAMLError:
                continue
            a = d.get("attributes") or {}
            if a.get("asset_name") in unit_assets or a.get("asset_key") in unit_assets:
                out.extend(_strings(d))
    for f in sorted((root / "scripts").glob("*.py")) if (root / "scripts").is_dir() else []:
        if any(f.stem == a or f.stem.startswith(a) for a in unit_assets):
            out.append(norm_command(f.read_text()))
    for f in sorted((root / "defs" / "python_assets").rglob("*.py")) if (root / "defs" / "python_assets").is_dir() else []:
        for chunk in re.split(r"(?m)^@dg\.asset\(", f.read_text())[1:]:
            m = re.search(r"name='([^']+)'|name=\"([^\"]+)\"", chunk)
            if m and (m.group(1) or m.group(2)) in unit_assets:
                out.append(norm_command(re.sub(r"(?m)^\s*#\s?", "", chunk)))
                out.extend(norm_command(x) for x in re.findall(r"'((?:[^'\\]|\\.)*)'", chunk))
    return [t for t in out if t]


def _strings(x: Any) -> list[str]:
    from .introspect import norm_command
    if isinstance(x, str):
        return [norm_command(x)]
    if isinstance(x, dict):
        return [t for v in x.values() for t in _strings(v)]
    if isinstance(x, list):
        return [t for v in x for t in _strings(v)]
    return []


def converter_side(spec: dict, project: Path, python: str, pkg: str | None = None) -> dict[str, Any]:
    project = Path(project).resolve()
    pkg = pkg or package_of(project)
    snap, err = introspect(project, python, pkg)
    if snap is None:
        return {"error": err}
    call_assets, assets = _converter_map(spec)
    unit_assets = set(assets) | {k for ks in call_assets.values() for k in ks}
    names = {n for r in spec["converter"]["rows"] if r["kind"] == "schedule" for e in r["expected"].get("schedules", []) for n in [e["name"]]}
    texts = converter_texts(project, pkg, unit_assets)
    m = measure(spec, snap, call_assets, unit_assets, lambda s: s["name"] in names, texts)
    have = {x["name"] for x in snap["sensors"]}
    m["file_event_sensors_missing"] = [r["source"] for r in spec["converter"]["rows"] if r["kind"] == "file_event" and r["expected"].get("sensor") not in have]
    m["stubs"] = [r["source"] for r in spec["converter"]["rows"] if r["caveat"] == "stub"]
    m["unenforced"] = sorted({n for r in spec["converter"]["rows"] for n in r["notes"] if NOT_ENFORCED.search(n)})
    return m


def rewrite_side(spec: dict, project: Path, python: str, pkg: str | None = None, unit_dir: Path | None = None) -> dict[str, Any]:
    ctx = Ctx(spec, project, python, pkg, unit_dir=unit_dir)
    snap, err = introspect(project, python, ctx.pkg)
    if snap is None:
        return {"error": err}
    ctx.snapshot = snap
    call_assets = ctx.call_assets()
    unit_assets = {k for ks in call_assets.values() for k in ks}
    job_assets = {j["name"]: set(j["assets"]) for j in snap["jobs"]}
    m = measure(spec, snap, call_assets, unit_assets, lambda s: bool(job_assets.get(s["job"], set()) & unit_assets), corpus(ctx.files),
                {c for c, ks in ctx.call_sensors().items() if ks})
    stubs = []
    from .accept import g_stubs
    if not g_stubs(ctx)["passed"]:
        stubs = ["a loud stub remains"]
    m["stubs"] = stubs
    have = {x["name"] for x in snap["sensors"]}
    m["file_event_sensors_missing"] = [f for f in spec["expected"]["file_events"] if not (ctx.acc.get(f) and ctx.acc[f]["assets"] and set(ctx.acc[f]["assets"]) <= have)]
    kinds = {"precondition", "status_handler", "wait_event", "notification"}
    m["constructs"] = {a["id"]: {"what": a["what"], "header": ctx.acc.get(a["id"], {}).get("text", "(not accounted for)")} for a in spec["accounting"] if a["kind"] in kinds}
    return m


def compare(spec: dict, conv: dict, rew: dict) -> dict[str, Any]:
    """Per dimension: the source's expectation, each side's result, and a verdict."""
    ex = spec["expected"]
    want_s = sorted((s["cron"], s["time_zone"]) for s in ex["schedules"])
    dims: list[dict] = []

    def add(name: str, want: Any, c_ok: bool, r_ok: bool, c_val: Any, r_val: Any, same: bool = False):
        verdict = "equivalent" if c_ok and r_ok else "equivalent (both differ from the source the same way)" if (not c_ok and not r_ok and (same or c_val == r_val)) else \
            "rewrite matches the source" if r_ok else "converter matches the source" if c_ok else "neither matches the source"
        dims.append({"dimension": name, "source": want, "converter": c_val, "rewrite": r_val, "converter_matches_source": c_ok, "rewrite_matches_source": r_ok, "verdict": verdict})

    add("graph order (earlier -> later pairs kept)", f"{len(ex['order_pairs'])} pair(s)", not conv["order_lost"], not rew["order_lost"],
        conv["order_lost"] or "all kept", rew["order_lost"] or "all kept")
    extra = len(ex["untranslatable_schedule_elements"])

    def covers(got: list) -> bool:
        rest = list(got)
        for w in want_s:
            if w not in rest:
                return False
            rest.remove(w)
        return len(rest) == extra
    c_extra = len(conv["schedules"]) - len(want_s)
    add("schedules (cron, time zone)" + (f"; {extra} element(s) have no cron form" if extra else ""), (want_s or "none") if not extra else f"{want_s} plus {extra} not expressible in cron",
        covers(conv["schedules"]), covers(rew["schedules"]), conv["schedules"] + ([f"{extra} element(s) dropped"] if extra and c_extra < extra else []), rew["schedules"])
    add("pools", ex["pools"] or "none", conv["pools"] == sorted(ex["pools"]), rew["pools"] == sorted(ex["pools"]), conv["pools"], rew["pools"])
    add("retries", "per call", not conv["retry_mismatch"], not rew["retry_mismatch"], conv["retry_mismatch"] or "match", rew["retry_mismatch"] or "match")
    add("commands and SQL present", f"{len(ex['commands'])} command(s)", not conv["commands_missing"], not rew["commands_missing"],
        conv["commands_missing"] or "all present", rew["commands_missing"] or "all present")
    if ex["notification_targets"]:
        add("notification targets (by name)", ex["notification_targets"], not conv["notification_targets_missing"], not rew["notification_targets_missing"],
            conv["notification_targets_missing"] or "all present", rew["notification_targets_missing"] or "all present")
    if ex["file_events"]:
        add("file events (a sensor each)", ex["file_events"], not conv["file_event_sensors_missing"], not rew["file_event_sensors_missing"],
            conv["file_event_sensors_missing"] or "all present", rew["file_event_sensors_missing"] or "all present")
    add("loud stubs left", "none wanted", not conv["stubs"], not rew["stubs"], conv["stubs"] or "none", rew["stubs"] or "none", same=bool(conv["stubs"]) == bool(rew["stubs"]))
    if rew.get("sensor_pairs_not_compared"):
        dims[0]["source"] = f"{dims[0]['source']}; {rew['sensor_pairs_not_compared']} involve a call the rewrite replaced with a sensor (not compared)"
    return {"unit": spec["unit"], "dimensions": dims, "assets": {"converter": conv["assets"], "rewrite": rew["assets"]},
            "checks": {"converter": len(conv["checks"]), "rewrite": len(rew["checks"])},
            "not_machine_compared": {"converter_unenforced": conv["unenforced"], "rewrite_constructs": rew.get("constructs", {})},
            "equivalent_on_all_machine_checked_dimensions": all(d["verdict"].startswith("equivalent") for d in dims)}


def render_md(results: list[dict]) -> str:
    L = ["# Equivalence between the converter route and the rewrite route", "",
         "Behavior, not code text. Each side is measured against the source facts in the spec; a dimension is *equivalent* when both sides match them (or differ identically). "
         "What replaced a precondition, status handler, event or notification is not machine-compared: the two statements are shown side by side. "
         "Also not machine-compared: timeouts, run windows, success exit codes and sensor settings (poll interval, minimum size and age): read them in the spec and in each side's YAML.", ""]
    for r in results:
        L += [f"## `{r['unit']}`", "", f"- assets: converter {len(r['assets']['converter'])} ({', '.join(r['assets']['converter']) or 'none'}); rewrite {len(r['assets']['rewrite'])} ({', '.join(r['assets']['rewrite']) or 'none'})",
              f"- asset checks: converter {r['checks']['converter']}, rewrite {r['checks']['rewrite']}",
              f"- equivalent on every machine-checked dimension: **{'yes' if r['equivalent_on_all_machine_checked_dimensions'] else 'no'}**", "",
              "| Dimension | Source | Converter | Rewrite | Verdict |", "|---|---|---|---|---|"]
        for d in r["dimensions"]:
            L.append(f"| {d['dimension']} | {d['source']} | {d['converter']} | {d['rewrite']} | {d['verdict']} |".replace("\n", " "))
        nm = r["not_machine_compared"]
        if nm["converter_unenforced"] or nm["rewrite_constructs"]:
            L += ["", "Not machine-compared:", ""]
            for n in nm["converter_unenforced"]:
                L.append(f"- converter: {n}")
            for k, v in nm["rewrite_constructs"].items():
                L.append(f"- rewrite, {v['what']}: {v['header']}")
        L.append("")
    return "\n".join(L)
