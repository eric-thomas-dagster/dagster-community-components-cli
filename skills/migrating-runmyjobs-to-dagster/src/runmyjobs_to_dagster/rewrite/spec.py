"""Deterministic, facts-only specification of each RunMyJobs unit (a chain or a standalone process definition), the input of the rewrite route.

`build_specs(model)` returns one spec dict per unit plus an estate dict. Everything in a spec is read from the extracted model (what RunMyJobs holds) and from what the
converter does with the unit today (translated, degraded or stubbed, and why). Purpose is stated only as what the facts show and is marked inferred. The same model gives
the same output (no timestamps, sorted lists). No secret value is written: a value whose name looks like a credential is replaced, and a credential-looking assignment in a
command is replaced too (the spec says how many); connections are NAMES only.

Spec format (kept generic so the other migration skills' rewrite routes can align): `<unit>.spec.json` (this dict) and `<unit>.spec.md` (the same facts as markdown),
`ESTATE.md` / `estate.json`, then `<unit>.prompt.md` and `<unit>.gates.json` from the later steps.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..convert import REVIEW, UNMAPPED, Result, _with_builtins, convert, slug
from ..cron import element_to_cron
from ..manifest import build_manifest
from ..model import Call, Chain, Model, ProcessDef

SPEC_VERSION = 1
SECRET_NAME = re.compile(r"pass(word|wd)?\b|secret|token|api_?key|credential|private|auth", re.I)
SECRET_CMD = [
    re.compile(r"""((?:\b\w*(?:pass(?:word|wd)?|secret|token|api_?key|credential|private_?key)\w*)\s*[:=]\s*)(['"]?)([^\s'";|&]{3,})\2""", re.I),
    re.compile(r"""(--?(?:pass(?:word|wd)?|secret|token|api-?key)[= ])(['"]?)([^\s'";|&]{3,})\2""", re.I),
]
PLACEHOLDER = "<redacted>"
FILE_OUTPUT = re.compile(r"^[\w\-./]*/[\w\-./]*\.(csv|txt|json|parquet|xml|dat|tsv|log)$", re.I)      # the only outputs whose check is derivable: a file path


def redact_text(text: str) -> tuple[str, int]:
    n = 0

    def sub(m: re.Match) -> str:
        nonlocal n
        v = m.group(3)
        if v.startswith(("$", "{", "<")):          # a reference to a variable, not a value
            return m.group(0)
        n += 1
        return f"{m.group(1)}{m.group(2)}{PLACEHOLDER}{m.group(2)}"
    for rx in SECRET_CMD:
        text = rx.sub(sub, text)
    return text, n


def mask_params(params: dict[str, Any]) -> dict[str, Any]:
    return {k: (PLACEHOLDER if SECRET_NAME.search(str(k)) and v not in (None, "") else v) for k, v in params.items()}


def norm_command(text: str) -> str:
    """Comparison form of a command or SQL text: whitespace collapsed and every parameter reference ($x, ${x}, {x}, {{ x }}) written as <x>."""
    t = re.sub(r"\$\{(\w+)\}|\$(\w+)|\{\{\s*(\w+)\s*\}\}|\{(\w+)\}", lambda m: f"<{(m.group(1) or m.group(2) or m.group(3) or m.group(4)).lower()}>", text or "")
    return " ".join(t.split())


@dataclass
class _Flat:
    id: str
    step: str
    call: Call
    process: str
    kind: str                       # process definition type, "chain" never appears (inlined), or "missing" / "recursive"
    path: list[str]
    upstream: list[str] = field(default_factory=list)
    values: dict[str, Any] = field(default_factory=dict)
    unresolved: list[str] = field(default_factory=list)
    step_handlers: list[dict] = field(default_factory=list)
    step_precondition: dict | None = None


def _called(model: Model) -> set[str]:
    return {c.process for ch in model.chains.values() for st in ch.steps for c in st.calls}


def units_of(model: Model) -> list[tuple[str, str]]:
    """(kind, name) of every rewrite unit: each non-inline chain, and each process definition that is not called from a chain or is scheduled directly."""
    out = [("chain", c.name) for c in model.chains.values() if not c.inline]
    called = _called(model)
    scheduled = {s.target for s in model.schedules} | {f.target for f in model.file_events}
    for p in model.processes.values():
        if p.name not in called or p.name in scheduled:
            out.append(("process_definition", p.name))
    return out


def flatten(chain: Chain, model: Model) -> tuple[list[_Flat], list[str]]:
    """Calls of a chain in the converter's order: a step's calls run in parallel and wait for every call of the previous step; a call to another chain is inlined."""
    out: list[_Flat] = []
    notes: list[str] = []

    def walk(ch: Chain, prefix: list[str], bound: dict, prev: list[str], stack: tuple[str, ...]) -> list[str]:
        for step in ch.steps:
            cur: list[str] = []
            for call in step.calls:
                path = prefix + [call.name]
                cid = f"{chain.name}/" + "/".join(path)
                values = dict(call.params)
                unresolved = []
                for local, src in call.param_refs.items():
                    if src in bound:
                        values[local] = bound[src]
                    else:
                        unresolved.append(f"{local} (from caller parameter {src})")
                if call.process in model.chains:
                    if call.process in stack or ch.name == call.process:
                        out.append(_Flat(cid, step.name, call, call.process, "recursive", path, list(prev), values, unresolved))
                        cur.append(cid)
                        notes.append(f"{cid}: chain {call.process} calls itself (directly or indirectly); it cannot be inlined")
                        continue
                    inner = model.chains[call.process]
                    inner_bound = {p.name: p.default for p in inner.parameters if p.default is not None}
                    inner_bound.update(values)
                    notes.append(f"{cid}: calls chain {call.process}; its steps are listed below with ids starting `{cid}/`")
                    cur.extend(walk(inner, path, inner_bound, prev, stack + (ch.name,)))
                    continue
                kind = model.processes[call.process].type.lower() if call.process in model.processes else "missing"
                out.append(_Flat(cid, step.name, call, call.process, kind, path, list(prev), values, unresolved, list(step.status_handlers), step.precondition))
                cur.append(cid)
            prev = cur or prev
        return prev

    walk(chain, [], {p.name: p.default for p in chain.parameters if p.default is not None}, [], (chain.name,))
    return out, notes


def _crons(sched) -> list[dict]:
    rows = []
    for el in sched.elements:
        try:
            cron, notes = element_to_cron(el)
        except Exception as e:  # noqa: BLE001
            cron, notes = None, [f"element {el!r} could not be translated: {e}"]
        rows.append({"element": el, "cron": cron, "converter_notes": notes})
    return rows


def _handler_text(h: dict) -> str:
    bits = [f"on status {h.get('status', '?')}: {h.get('action', '?')}"]
    for k in ("max", "delay_seconds", "message", "return_code", "step"):
        if h.get(k) not in (None, ""):
            bits.append(f"{k}={h[k]}")
    return ", ".join(bits)


def _pre_text(pre: dict | None) -> str:
    if not pre:
        return ""
    bits = []
    if pre.get("time_window"):
        bits.append(f"time window {pre['time_window']} must be {str(pre.get('status', 'IsOpen')).replace('Is', '').lower() or 'open'}")
    if pre.get("time_zone"):
        bits.append(f"time zone {pre['time_zone']}")
    if pre.get("expression"):
        bits.append(f"expression {pre['expression']!r}")
    return "; ".join(bits) or repr(pre)


def _converter_view(result: Result, manifest: dict, model: Model, kind: str, name: str, flat: list[_Flat]) -> dict:
    """What the converter (components output, default settings) does with this unit, from its report items and manifest."""
    by_item = {id(it): u for it, u in zip(result.items, manifest["units"])}
    rows = []
    mine: list[tuple[Any, dict]] = []
    sched_names = {s.name for s in model.schedules if s.target == name}
    fe_names = {f.name for f in model.file_events if f.target == name}
    for it in result.items:
        u = by_item[id(it)]
        if (it.kind == "chain" and it.source == name and kind == "chain") \
                or (it.kind == "process_definition" and ((kind == "chain" and it.source.startswith(name + "/")) or (kind == "process_definition" and it.source == name))) \
                or (it.kind == "schedule" and it.source in sched_names) or (it.kind == "file_event" and it.source in fe_names):
            mine.append((it, u))
    for it, u in mine:
        rows.append({"manifest_id": u["id"], "kind": it.kind, "source": it.source, "artifact": it.artifact, "status": it.status, "caveat": it.caveat,
                     "notes": list(it.notes), "expected": it.expected})
    stubbed = [r["source"] for r in rows if r["caveat"] == "stub"]
    degraded = [r["source"] for r in rows if r["caveat"] == "degraded"]
    chain_row = next((r for r in rows if r["kind"] == "chain"), None)
    process_row = next((r for r in rows if r["kind"] == "process_definition" and kind == "process_definition"), None)
    head = chain_row or process_row
    collapsed = bool(chain_row and "TaskChainAssetComponent" in chain_row["artifact"])
    assets = list((chain_row or {}).get("expected", {}).get("assets") or ([process_row["expected"]["asset"]] if process_row and process_row["expected"].get("asset") else []))
    status = head["status"] if head else "none"
    if status == "converted" and not stubbed and not degraded:
        disposition = "translated"
    elif stubbed:
        disposition = "stubbed"
    else:
        disposition = "degraded"
    why = sorted({n for r in rows for n in r["notes"] if r["caveat"] in ("stub", "degraded") or r["kind"] in ("chain", "schedule", "file_event")})
    return {"disposition": disposition, "status": status, "collapsed_to_one_asset": collapsed, "assets": assets, "stubbed": stubbed, "degraded": degraded,
            "reasons": why, "rows": rows, "manifest_units": [r["manifest_id"] for r in rows]}


def _call_spec(model: Model, f: _Flat, view: dict, chain: Chain) -> dict:
    proc: ProcessDef | None = model.processes.get(f.process)
    params = {}
    if proc:
        params = {p.name: p.default for p in proc.parameters if p.direction in ("in", "in_out")}
    params.update(f.values)
    cmd, redacted = redact_text(proc.command) if proc else ("", 0)
    conv_rows = [r for r in view["rows"] if r["kind"] == "process_definition" and r["source"].startswith(f"{f.id.split('/')[0]}/{f.call.name} (")]
    spec = {
        "id": f.id, "step": f.step, "call": f.call.name, "process_definition": f.process, "type": f.kind,
        "upstream": f.upstream, "parameters": mask_params({k: ("" if v is None else v) for k, v in params.items()}),
        "parameters_taken_from_caller": f.unresolved,
        "command": cmd, "command_redacted": redacted, "command_normalized": norm_command(cmd) if not redacted else "",
        "application": proc.application if proc else "", "description": proc.description if proc else "",
        "queue": proc.queue if proc else "", "pool_name_used_by_converter": slug(proc.queue) if proc and proc.queue else "",
        "max_starts": proc.max_starts if proc else None, "timeout_seconds": proc.timeout_seconds if proc else None,
        "run_as": proc.run_as if proc else "", "connection": proc.connection if proc else "", "time_zone": proc.time_zone if proc else "",
        "outputs": list(proc.outputs) if proc else [], "success_exit_codes": list(proc.success_exit_codes) if proc else [],
        "run_window": proc.run_window if proc else "", "extraction_notes": [n for n in (proc.extraction_notes if proc else []) if not n.startswith("source: ")],
        "wait_events": list(f.call.wait_events), "precondition": f.call.precondition,
        "step_status_handlers": f.step_handlers, "step_precondition": f.step_precondition,
        "converter": [{"manifest_id": r["manifest_id"], "artifact": r["artifact"], "status": r["status"], "caveat": r["caveat"], "notes": r["notes"]} for r in conv_rows],
    }
    if f.kind == "missing":
        spec["missing_definition"] = "not in the export (a built-in with no mapping, or export it too)"
    if f.kind == "mail":
        low = {k.lower(): v for k, v in params.items()}
        spec["notification"] = {"channel": "email", "to": low.get("to", ""), "cc": low.get("cc", ""), "bcc": low.get("bcc", ""), "subject": low.get("subject", "")}
    if f.kind == "sap":
        spec["sap"] = {"target": f.process, "parameters": mask_params({k: v for k, v in params.items() if v not in ("", None)})}
    return spec


def _purpose(kind: str, name: str, chain: Chain | None, proc: ProcessDef | None, calls: list[dict], scheds: list[dict], fes: list[dict]) -> list[dict]:
    out: list[dict] = []
    if chain is not None:
        types = sorted({c["type"] for c in calls})
        out.append({"text": f"a chain of {len(chain.steps)} step(s) with {len(calls)} call(s) of type(s): {', '.join(types) or 'none'}", "inferred": False})
        if chain.description:
            out.append({"text": f"its own description reads: {chain.description!r}", "inferred": False})
        last = [c["id"] for c in calls if not any(c["id"] in d["upstream"] for d in calls)]
        if last:
            out.append({"text": f"its final call(s) are {', '.join(last)} (calls nothing else waits for)", "inferred": False})
        outs = [o for c in calls for o in c["outputs"]]
        if outs:
            out.append({"text": f"declared outputs of its processes: {', '.join(sorted(set(outs)))}", "inferred": False})
        if any(c["type"] == "sql" for c in calls) and any(c["type"] in ("os", "python") for c in calls):
            out.append({"text": "it moves or prepares data with scripts, then writes it with SQL, a load-then-transform shape", "inferred": True})
        if calls and all(c["type"] == "os" and c["command"].startswith("echo System_DoNothing") for c in calls):
            out.append({"text": "every call is the RunMyJobs no-op built-in System_DoNothing: the chain is an orchestration skeleton (placeholders for later jobs)", "inferred": True})
        if any(c["wait_events"] for c in calls) or any("WaitEvent" in n or "waits for event" in n for n in chain.notes):
            out.append({"text": "it waits for an event before part of it runs, so it is started or gated by something outside this export", "inferred": True})
    elif proc is not None:
        out.append({"text": f"a standalone {proc.type} process definition in application {proc.application or '(none)'}", "inferred": False})
        if proc.description:
            out.append({"text": f"its own description reads: {proc.description!r}", "inferred": False})
    if scheds:
        out.append({"text": f"it is started by {len(scheds)} submit frame(s): {', '.join(s['name'] for s in scheds)}", "inferred": False})
    if fes:
        out.append({"text": f"it is started by file event(s): {', '.join(f['name'] for f in fes)}", "inferred": False})
    if not scheds and not fes:
        out.append({"text": "no submit frame or file event in the export starts it (it may be started by a person, another chain or an event)", "inferred": True})
    return out


MUST_PRESERVE = [
    "the order of steps and which calls may run in parallel (calls in one step run together; every call waits for all calls of the step before)",
    "every command, SQL statement and script exactly as given (the spec lists them verbatim), and the parameters passed to them",
    "the schedule: cron meaning, time zone, and any end date (a submit frame with several elements is a union of cycles)",
    "the concurrency each queue enforces (queue limits are shared across units, see ESTATE.md)",
    "retries from restart handlers and from Maximum Starts, timeouts, and which exit codes count as success",
    "what stops the run (a failing step stops the chain) and what continues (a status handler that marks a step completed on error)",
    "notification targets (who is emailed) and connection names; secrets and credentials come from the environment, never from code",
]
ACCEPTABLE = [
    "asset boundaries and names: one asset per data product or job outcome is fine; steps that only prepare an output may fold into the asset that uses it",
    "a chain may become asset dependencies, a task-chain component, or one asset; it must keep the order above",
    "queues become Dagster pools (the limit is configured on the instance, not in code); schedules become Dagster schedules with an explicit time zone",
    "status handlers, preconditions and notifications may become hooks, sensors, asset checks, automation conditions or alerts instead of steps",
    "RunMyJobs-only constructs with no Dagster counterpart (run_as user, process server, priority) may be dropped if the header docstring says so",
    "'last day of month' and minute-level time windows have no exact cron form: a schedule function or an in-asset window check is acceptable, a silent change is not",
]



def _render(text: str, params: dict) -> str:
    import string
    return string.Template(text).safe_substitute({k: v for k, v in params.items()})


def _retries(c: dict) -> int:
    """Retries the converter gives a call: a restart handler's maximum on its step wins; otherwise Maximum Starts - 1 (the meaning of Maximum Starts is unconfirmed)."""
    for h in c["step_status_handlers"]:
        if h.get("action") == "restart_step" and str(h.get("status", "")).lower() in ("error", "failed"):
            return int(h.get("max", 1))
    return c["max_starts"] - 1 if c["max_starts"] and c["max_starts"] > 1 else 0


def build_unit(model: Model, result: Result, manifest: dict, kind: str, name: str, queues_used: dict[str, list[str]]) -> dict:
    chain = model.chains.get(name) if kind == "chain" else None
    proc = model.processes.get(name) if kind == "process_definition" else None
    if chain is not None:
        flat, flat_notes = flatten(chain, model)
    else:
        flat, flat_notes = [], []
    view = _converter_view(result, manifest, model, kind, name, flat)
    if proc is not None:
        fl = _Flat(name, "", Call(name=name, process=name), name, proc.type.lower(), [name])
        calls = [_call_spec(model, fl, view, Chain(name=name)) | {"id": name}]
        # a standalone process's converter row uses the bare process name as its source
        calls[0]["converter"] = [{"manifest_id": r["manifest_id"], "artifact": r["artifact"], "status": r["status"], "caveat": r["caveat"], "notes": r["notes"]}
                                 for r in view["rows"] if r["kind"] == "process_definition"]
    else:
        calls = [_call_spec(model, f, view, chain) for f in flat]
    scheds = []
    for s in model.schedules:
        if s.target != name:
            continue
        cr = _crons(s)
        scheds.append({"name": s.name, "time_zone": s.time_zone or "UTC", "end": s.end, "elements": cr,
                       "crons": [c["cron"] for c in cr if c["cron"]], "untranslatable": [c["element"] for c in cr if not c["cron"]],
                       "converter_schedule_names": [e["name"] for r in view["rows"] if r["kind"] == "schedule" and r["source"] == s.name for e in r["expected"].get("schedules", [])]})
    fes = [{"name": f.name, "directory": f.directory, "pattern": f.pattern, "poll_seconds": f.poll_seconds, "min_size_bytes": f.min_size_bytes, "min_age_seconds": f.min_age_seconds,
            "stable": f.stable, "recursive": f.recursive, "one_run_per_poll": f.one_run_per_poll} for f in model.file_events if f.target == name]
    steps = []
    if chain is not None:
        for st in chain.steps:
            steps.append({"name": st.name, "calls": [c["id"] for c in calls if c["step"] == st.name and len(c["id"].split("/")) == 2],
                          "precondition": st.precondition, "status_handlers": st.status_handlers})
    qnames = sorted({c["queue"] for c in calls if c["queue"]})
    queues = [{"name": q, "pool": slug(q), "max_concurrency": model.queues[q].max_concurrency if q in model.queues else None, "process_server": model.queues[q].process_server if q in model.queues else "",
               "declared_in_export": q in model.queues, "shared_with_units": sorted(u for u in queues_used.get(q, []) if u != name),
               "used_by_calls": [c["id"] for c in calls if c["queue"] == q]} for q in qnames]
    pre = []
    if chain is not None and chain.precondition:
        pre.append({"where": f"chain {name}", "text": _pre_text(chain.precondition)})
    for st in (chain.steps if chain else []):
        if st.precondition:
            pre.append({"where": f"step {st.name}", "text": _pre_text(st.precondition)})
    for c in calls:
        if c["precondition"]:
            pre.append({"where": f"call {c['id']}", "text": _pre_text(c["precondition"])})
    handlers = [{"where": f"step {st.name}", "text": _handler_text(h), "raw": h} for st in (chain.steps if chain else []) for h in st.status_handlers]
    events = [{"where": c["id"], "event": w.get("event"), "clears": w.get("clears")} for c in calls for w in c["wait_events"]]
    notes_from_extractor = list(chain.notes) if chain else []
    window = (chain.run_window if chain else (proc.run_window if proc else ""))
    notifications = [c["notification"] | {"call": c["id"]} for c in calls if c.get("notification")]
    connections = sorted({c["connection"] for c in calls if c["connection"]} | ({"(default for queue " + c["queue"] + ")" for c in calls if c["type"] == "sql" and not c["connection"] and c["queue"]}))
    tzs = sorted({s["time_zone"] for s in scheds} | {c["time_zone"] for c in calls if c["time_zone"]} | ({w.split(" ")[1] for w in [window] if window and len(w.split(" ")) > 1 and "/" in w.split(" ")[1]}))
    accounting = []
    for c in calls:
        accounting.append({"id": c["id"], "kind": "call", "what": f"{c['process_definition']} ({c['type']})"})
    for s in scheds:
        accounting.append({"id": s["name"], "kind": "schedule", "what": f"submit frame, time zone {s['time_zone']}"})
    for q in queues:
        accounting.append({"id": q["name"], "kind": "queue", "what": f"queue (pool {q['pool']})"})
    for f in fes:
        accounting.append({"id": f["name"], "kind": "file_event", "what": f"file event on {f['directory']}"})
    for i, p_ in enumerate(pre):
        accounting.append({"id": f"{name}#precondition{i + 1}", "kind": "precondition", "what": f"{p_['where']}: {p_['text']}"})
    for i, h in enumerate(handlers):
        accounting.append({"id": f"{name}#handler{i + 1}", "kind": "status_handler", "what": f"{h['where']}: {h['text']}"})
    for i, ev in enumerate(events):
        accounting.append({"id": f"{name}#wait{i + 1}", "kind": "wait_event", "what": f"{ev['where']} waits for event {ev['event']}"})
    if window:
        accounting.append({"id": f"{name}#run_window", "kind": "run_window", "what": f"{'chain' if chain else 'process'} runs only inside {window}; outside it the run is skipped (not failed)"})
    for c in calls:
        if c["run_window"] and c["run_window"] != window:
            accounting.append({"id": f"{c['id']}#run_window", "kind": "run_window", "what": f"call runs only inside {c['run_window']}; outside it the run is skipped (not failed)"})
    for i, n in enumerate(notes_from_extractor):
        accounting.append({"id": f"{name}#note{i + 1}", "kind": "extractor_note", "what": f"the extractor saw but did not convert: {n}"})
    for i, n in enumerate(notifications):
        accounting.append({"id": f"{name}#notify{i + 1}", "kind": "notification", "what": f"email from {n['call']} to {n['to'] or '(set at run time)'}"})
    order_pairs = [[u, c["id"]] for c in calls for u in c["upstream"]]
    expected = {
        "order_pairs": order_pairs,                          # (earlier call, later call): the later must be downstream of the earlier, or both in one asset
        "schedules": [{"cron": cr, "time_zone": s["time_zone"], "name": s["name"]} for s in scheds for cr in s["crons"]],
        "untranslatable_schedule_elements": [e for s in scheds for e in s["untranslatable"]],
        "pools": sorted({q["pool"] for q in queues}),
        "commands": [{"call": c["id"], "type": c["type"], "normalized": c["command_normalized"], "rendered": norm_command(_render(c["command"], c["parameters"])) if not c["command_redacted"] else ""}
                     for c in calls if c["command_normalized"] and c["type"] in ("os", "python", "sql", "redwoodscript")],
        "notification_targets": sorted({a.strip().lower() for n in notifications for a in re.split(r"[,;]", f"{n['to']},{n['cc']},{n['bcc']}") if a.strip() and "$" not in a}),
        "retries": {c["id"]: _retries(c) for c in calls},
        "file_events": [f["name"] for f in fes],
        "structural_checks": [{"call": c["id"], "output": o, "check": "file exists and is not empty"} for c in calls for o in c["outputs"] if FILE_OUTPUT.match(o)],
        "external_systems": sorted({c["type"] for c in calls if c["type"] in ("sap", "mail", "sql", "missing", "recursive")} | ({"dbt_cloud/hvr (heuristic)"} if any("recogni" in " ".join(n for r in c["converter"] for n in r["notes"]) for c in calls) else set())),
    }
    spec = {
        "version": SPEC_VERSION, "unit": name, "unit_id": slug(name), "kind": kind, "application": (chain.application if chain else (proc.application if proc else "")),
        "description": (chain.description if chain else (proc.description if proc else "")),
        "purpose": _purpose(kind, name, chain, proc, calls, scheds, fes),
        "parameters": [{"name": p.name, "direction": p.direction, "type": p.type, "default": ("" if p.default is None else p.default) if not SECRET_NAME.search(p.name) else PLACEHOLDER,
                        "has_default": p.default is not None, "mandatory": p.mandatory} for p in ((chain.parameters if chain else (proc.parameters if proc else [])))],
        "steps": steps, "calls": calls, "edges": order_pairs, "flatten_notes": flat_notes,
        "schedules": scheds, "file_events": fes, "queues": queues, "preconditions": pre, "status_handlers": handlers, "wait_events": events,
        "notifications": notifications, "connections": connections, "time_zones": tzs, "run_window": window, "extractor_notes": notes_from_extractor,
        "accounting": accounting, "expected": expected,
        "must_preserve": MUST_PRESERVE, "acceptable_differences": ACCEPTABLE,
        "converter": {k: view[k] for k in ("disposition", "status", "collapsed_to_one_asset", "assets", "stubbed", "degraded", "reasons", "rows", "manifest_units")},
        "manifest_units": view["manifest_units"],
        "queue_manifest_units": [u["id"] for u in manifest["units"] if u["kind"] == "queue" and u["source"] in qnames],
    }
    return spec


def build_estate(model: Model, specs: dict[str, dict], result: Result, manifest: dict) -> dict:
    by_chain_calls: dict[str, list[str]] = {}
    for ch in model.chains.values():
        by_chain_calls[ch.name] = sorted({c.process for st in ch.steps for c in st.calls if c.process in model.chains})
    chain_calls = {k: v for k, v in by_chain_calls.items() if v}
    called_by: dict[str, list[str]] = {}
    for a, bs in chain_calls.items():
        for b in bs:
            called_by.setdefault(b, []).append(a)
    inline = sorted(c.name for c in model.chains.values() if c.inline)
    qu: dict[str, list[str]] = {}
    for u, s in specs.items():
        for q in s["queues"]:
            qu.setdefault(q["name"], []).append(u)
    proc_uses: dict[str, list[str]] = {}
    for u, s in specs.items():
        for c in s["calls"]:
            if c["process_definition"] in model.processes:
                proc_uses.setdefault(c["process_definition"], []).append(u)
    shared_defs = {p: sorted(set(us)) for p, us in proc_uses.items() if len(set(us)) > 1}
    out_users: dict[str, list[str]] = {}
    for u, s in specs.items():
        for c in s["calls"]:
            for o in c["outputs"]:
                out_users.setdefault(o, []).append(u)
    shared_outputs = {o: sorted(set(us)) for o, us in out_users.items() if len(set(us)) > 1}
    sched_by_target: dict[str, list[str]] = {}
    for s in model.schedules:
        sched_by_target.setdefault(s.target, []).append(s.name)
    events_waited: dict[str, list[str]] = {}
    for u, s in specs.items():
        for e in s["wait_events"]:
            events_waited.setdefault(str(e["event"]), []).append(u)
        for n in s["extractor_notes"]:
            m = re.search(r"waits for event '([^']+)'", n)
            if m:
                events_waited.setdefault(m.group(1), []).append(u)
    roots = sorted(u for u in specs if u not in called_by and specs[u]["kind"] == "chain")
    candidates = []
    for parent, kids in sorted(chain_calls.items()):
        if parent in specs and all(k in specs or model.chains[k].inline for k in kids):
            candidates.append({"inferred": True, "text": f"chain {parent} runs chain(s) {', '.join(kids)}: either one pipeline whose assets are the kids' assets in order (asset dependencies across the units), or the parent as a thin orchestration job over kid pipelines that stay separate"})
    for q, us in sorted(qu.items()):
        if len(us) > 1:
            candidates.append({"inferred": True, "text": f"queue {q} is used by {', '.join(sorted(us))}: one pool `{slug(q)}` with one limit shared by all of them (not one pool per unit)"})
    for p, us in sorted(shared_defs.items()):
        candidates.append({"inferred": True, "text": f"definition {p} is called from {', '.join(us)}: one reusable function or component configured per caller, not copied"})
    for o, us in sorted(shared_outputs.items()):
        candidates.append({"inferred": True, "text": f"output {o} is declared by processes in {', '.join(us)}: probably ONE asset that several units refresh; confirm who owns it"})
    for ev, us in sorted(events_waited.items()):
        candidates.append({"inferred": True, "text": f"event {ev} gates {', '.join(sorted(set(us)))}; the export does not show what raises it"})
    return {
        "version": SPEC_VERSION, "units": sorted(specs), "unit_kinds": {u: s["kind"] for u, s in sorted(specs.items())},
        "chains_calling_chains": chain_calls, "chains_called_by": {k: sorted(v) for k, v in sorted(called_by.items())}, "inline_wrapper_chains": inline,
        "top_level_chains": roots, "queues": {q: {"pool": slug(q), "max_concurrency": model.queues[q].max_concurrency if q in model.queues else None, "units": sorted(us)} for q, us in sorted(qu.items())},
        "shared_process_definitions": shared_defs, "shared_declared_outputs": shared_outputs, "schedules_by_target": {k: sorted(v) for k, v in sorted(sched_by_target.items())},
        "events_waited_for": {k: sorted(set(v)) for k, v in sorted(events_waited.items())}, "declared_events": sorted(model.events),
        "unused_queues": sorted(q for q in model.queues if q not in qu),
        "candidate_boundaries": candidates,
        "converter_dispositions": {u: s["converter"]["disposition"] for u, s in sorted(specs.items())},
        "integrations": [{"source": it.source, "artifact": it.artifact, "notes": it.notes} for it in result.items if it.kind == "integration"],
        "queue_units": [{"manifest_id": u["id"], "source": u["source"]} for u in manifest["units"] if u["kind"] == "queue"],
    }


def build_specs(model: Model, pkg: str = "rmj_migrated") -> tuple[dict[str, dict], dict, dict]:
    """(specs by unit name, estate, manifest) from the extracted model. The manifest is the converter's (components output), used to say what the converter does."""
    m = _with_builtins(model)
    result = convert(model, output="components", pkg=pkg)
    manifest = build_manifest(result)
    units = units_of(m)
    queues_used: dict[str, list[str]] = {}
    # which units use which queue (needs flattening once)
    for kind, name in units:
        if kind == "chain":
            for f in flatten(m.chains[name], m)[0]:
                p = m.processes.get(f.process)
                if p and p.queue:
                    queues_used.setdefault(p.queue, []).append(name)
        else:
            p = m.processes[name]
            if p.queue:
                queues_used.setdefault(p.queue, []).append(name)
    specs = {name: build_unit(m, result, manifest, kind, name, queues_used) for kind, name in units}
    return specs, build_estate(m, specs, result, manifest), manifest


# ---------------------------------------------------------------- markdown


def _j(v: Any) -> str:
    import json
    return json.dumps(v, sort_keys=True, default=str)


def render_spec_md(s: dict) -> str:
    L: list[str] = []
    A = L.append
    A(f"# Specification: RunMyJobs {s['kind'].replace('_', ' ')} `{s['unit']}`")
    A("")
    A("Facts read from the extracted model (what RunMyJobs holds) and from the converter's report (what the converter does with this unit today). Statements marked *inferred* are not facts. "
      "No secret value is in this file: credential-like parameter values and credential-looking assignments in commands are replaced, and connections are names only.")
    A("")
    A(f"- application: {s['application'] or '(none)'}; description: {s['description'] or 'none'}; unit id `{s['unit_id']}`")
    A(f"- time zones named: {', '.join(s['time_zones']) or 'none'}; run window (chain or process): {s['run_window'] or 'none'}")
    A("")
    A("## What it does (from the facts)")
    A("")
    for p in s["purpose"]:
        A(f"- {p['text']}" + (" *inferred*" if p["inferred"] else ""))
    A("")
    A("## What the converter does with it today")
    A("")
    cv = s["converter"]
    A(f"- disposition: **{cv['disposition']}** (report status `{cv['status']}`); collapsed to one asset: {cv['collapsed_to_one_asset']}; assets: {', '.join(cv['assets']) or 'none'}")
    A(f"- stubbed (fails with RMJ-CAVEAT until ported): {', '.join(cv['stubbed']) or 'none'}")
    A(f"- degraded (translated with a stated difference): {', '.join(cv['degraded']) or 'none'}")
    for r in cv["reasons"]:
        A(f"  - {r}")
    A(f"- manifest units: {', '.join(cv['manifest_units']) or 'none'}")
    A("")
    A("## Parameters")
    A("")
    if s["parameters"]:
        for p in s["parameters"]:
            A(f"- `{p['name']}` ({p['direction']}, {p['type']}): default " + (f"`{p['default']}`" if p["has_default"] else "none") + ("; mandatory" if p["mandatory"] else ""))
    else:
        A("- none")
    A("")
    A("## Graph")
    A("")
    if s["steps"]:
        A("Steps run in order; calls in one step run in parallel, and every call waits for all calls of the step before.")
        A("")
        for i, st in enumerate(s["steps"], 1):
            A(f"{i}. step `{st['name']}`: " + (", ".join(f"`{c}`" for c in st["calls"]) or "(no calls)"))
    A(f"- edges (earlier -> later): " + (", ".join(f"{a} -> {b}" for a, b in s["edges"]) or "none"))
    for n in s["flatten_notes"]:
        A(f"- {n}")
    A("")
    A("## Calls")
    for c in s["calls"]:
        A("")
        A(f"### `{c['id']}` ({c['type']}, definition `{c['process_definition']}`)")
        A("")
        A(f"- step: {c['step'] or '(standalone)'}; upstream: {', '.join(c['upstream']) or 'none'}; application {c['application'] or '(none)'}")
        A(f"- queue: {c['queue'] or 'none'}" + (f" (the converter makes it pool `{c['pool_name_used_by_converter']}`)" if c['queue'] else "")
          + f"; Maximum Starts: {c['max_starts']} (retries here = {c['max_starts'] - 1 if c['max_starts'] and c['max_starts'] > 1 else 0}, the meaning of Maximum Starts is unconfirmed); timeout: {c['timeout_seconds']} s; run as: {c['run_as'] or 'default'} (not enforceable in Dagster)")
        A(f"- connection (name): {c['connection'] or 'none'}; success exit codes besides 0: {c['success_exit_codes'] or 'none'}; run window: {c['run_window'] or 'none'}; declared outputs: {', '.join(c['outputs']) or 'none'}")
        A(f"- resolved parameters: `{_j(c['parameters'])}`" + (f"; taken from the caller (no value here): {', '.join(c['parameters_taken_from_caller'])}" if c["parameters_taken_from_caller"] else ""))
        if c["precondition"]:
            A(f"- precondition: {_pre_text(c['precondition'])}")
        for w in c["wait_events"]:
            A(f"- waits for event `{w.get('event')}`" + (" and clears it" if w.get("clears") else ""))
        for h in c["step_status_handlers"]:
            A(f"- step status handler: {_handler_text(h)}")
        if c.get("notification"):
            A(f"- notification: email to `{c['notification']['to'] or '(set at run time)'}` cc `{c['notification']['cc'] or '-'}` subject `{c['notification']['subject']}`")
        if c.get("missing_definition"):
            A(f"- the definition is {c['missing_definition']}")
        for n in c["extraction_notes"]:
            A(f"- extractor note: {n}")
        for r in c["converter"]:
            A(f"- converter: {r['manifest_id']} -> {r['artifact']} [{r['status']}{'/' + r['caveat'] if r['caveat'] else ''}]" + ("; " + "; ".join(r["notes"]) if r["notes"] else ""))
        if c["command"]:
            A("- command / SQL / script (verbatim" + (f"; {c['command_redacted']} credential-looking value(s) replaced" if c["command_redacted"] else "") + "):")
            A("")
            A("```")
            A(c["command"])
            A("```")
    A("")
    A("## Schedules and calendars")
    A("")
    if s["schedules"]:
        for sc in s["schedules"]:
            A(f"- `{sc['name']}`: time zone `{sc['time_zone']}`" + (f", ends {sc['end']}" if sc["end"] else "") + f"; elements: `{_j([e['element'] for e in sc['elements']])}`")
            for e in sc["elements"]:
                A(f"  - element {_j(e['element'])} -> cron `{e['cron']}`" if e["cron"] else f"  - element {_j(e['element'])} has NO cron form")
                for n in e["converter_notes"]:
                    A(f"    - {n}")
    else:
        A("- none in the export (the extractor warns when a CAR export holds no submit frames; named calendars are not parsed)")
    for f in s["file_events"]:
        A(f"- file event `{f['name']}`: directory `{f['directory']}`, pattern `{f['pattern']}`, poll {f['poll_seconds']} s, min size {f['min_size_bytes']}, min age {f['min_age_seconds']}, stable-size check {f['stable']}")
    A("")
    A("## Queues")
    A("")
    if s["queues"]:
        for q in s["queues"]:
            A(f"- queue `{q['name']}` -> pool `{q['pool']}`: limit {q['max_concurrency'] if q['max_concurrency'] else 'not declared'} (caps steps running at once across all runs; the limit is set on the Dagster instance); "
              f"declared in export: {q['declared_in_export']}; used by {', '.join(q['used_by_calls'])}; shared with units: {', '.join(q['shared_with_units']) or 'none'}")
    else:
        A("- none")
    A("")
    A("## Preconditions, status handlers, events, notifications")
    A("")
    for p in s["preconditions"]:
        A(f"- precondition at {p['where']}: {p['text']} (the time window's own definition is not in the export)")
    for h in s["status_handlers"]:
        A(f"- status handler at {h['where']}: {h['text']}")
    for e in s["wait_events"]:
        A(f"- wait event `{e['event']}` at {e['where']}")
    for n in s["extractor_notes"]:
        A(f"- extractor note on the chain: {n}")
    for n in s["notifications"]:
        A(f"- notification from {n['call']}: to `{n['to'] or '(set at run time)'}`")
    if not (s["preconditions"] or s["status_handlers"] or s["wait_events"] or s["extractor_notes"] or s["notifications"]):
        A("- none")
    A("")
    A("## Connections and endpoints (names only)")
    A("")
    A("- " + (", ".join(f"`{c}`" for c in s["connections"]) if s["connections"] else "none") + ". The converter reads a SQLAlchemy URL from `RMJ_CONN_<NAME>`; SAP and SMTP settings come from `SAP_*` / `SMTP_*` environment variables. Never write a value.")
    A("")
    A("## Behaviours that must be preserved")
    A("")
    for m in s["must_preserve"]:
        A(f"- {m}")
    A("")
    A("## Differences that are acceptable")
    A("")
    for m in s["acceptable_differences"]:
        A(f"- {m}")
    A("")
    A("## Objects the header docstring must account for")
    A("")
    A("One line per object in the form `<id>: replaced by `<asset key>`` / `folded into `<asset key>`` / `merged into `<asset key>`` / `dropped: <reason>` (a call whose text is not kept verbatim says `reimplemented`).")
    A("")
    for a in s["accounting"]:
        A(f"- `{a['id']}` ({a['kind']}): {a['what']}")
    A("")
    return "\n".join(L)


def render_estate_md(e: dict, specs: dict[str, dict]) -> str:
    L: list[str] = ["# Estate: how the units relate", "",
                    "Facts from the extracted model, so a rewrite can choose asset boundaries across units. Lines marked *inferred* are suggestions to confirm, not facts.", ""]
    A = L.append
    A(f"## Units ({len(e['units'])})")
    A("")
    for u in e["units"]:
        s = specs[u]
        A(f"- `{u}` ({e['unit_kinds'][u]}): {len(s['calls'])} call(s), converter disposition **{e['converter_dispositions'][u]}**, schedules: {', '.join(x['name'] for x in s['schedules']) or 'none'}, queues: {', '.join(q['name'] for q in s['queues']) or 'none'}")
    A("")
    A("## Chains calling chains")
    A("")
    for p, ks in e["chains_calling_chains"].items():
        A(f"- `{p}` runs {', '.join(f'`{k}`' for k in ks)}")
    if not e["chains_calling_chains"]:
        A("- none")
    if e["inline_wrapper_chains"]:
        A(f"- inline wrapper chains (System_Internal_Chain<n>; they only run inside their parent): {', '.join(e['inline_wrapper_chains'])}")
    A(f"- top-level chains (nothing in the export calls them): {', '.join(e['top_level_chains']) or 'none'}")
    A("")
    A("## Shared queues")
    A("")
    for q, d in e["queues"].items():
        A(f"- queue `{q}` -> pool `{d['pool']}`, limit {d['max_concurrency'] or 'not declared'}: units {', '.join(d['units'])}")
    if not e["queues"]:
        A("- none")
    A("")
    if e["unused_queues"]:
        A(f"- queues declared in the export that no call uses: {', '.join(e['unused_queues'])}. They belong to no unit, so no rewrite gate completes them: give each a disposition with `status advance` (say where the pool is created) or `status defer` (say why it is not needed)")
    A("")
    A("## Shared definitions and outputs")
    A("")
    for p, us in e["shared_process_definitions"].items():
        A(f"- definition `{p}` is called from {', '.join(us)}")
    for o, us in e["shared_declared_outputs"].items():
        A(f"- declared output `{o}` appears in {', '.join(us)}")
    if not e["shared_process_definitions"] and not e["shared_declared_outputs"]:
        A("- none")
    A("")
    A("## Schedules, events")
    A("")
    for t, ss in e["schedules_by_target"].items():
        A(f"- `{t}` is submitted by {', '.join(ss)}")
    for ev, us in e["events_waited_for"].items():
        A(f"- event `{ev}` is waited for by {', '.join(us)}; the export does not show what raises it")
    if not e["schedules_by_target"] and not e["events_waited_for"]:
        A("- none")
    A("")
    A("## Candidate asset boundaries (inferred)")
    A("")
    for c in e["candidate_boundaries"]:
        A(f"- {c['text']} *inferred*")
    if not e["candidate_boundaries"]:
        A("- none")
    A("")
    if e["integrations"]:
        A("## Integrations the converter adds")
        A("")
        for i in e["integrations"]:
            A(f"- {i['source']} -> {i['artifact']}")
        A("")
    return "\n".join(L)
