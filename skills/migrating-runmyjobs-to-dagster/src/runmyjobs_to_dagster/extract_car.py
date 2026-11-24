"""Extract a Model from a RunMyJobs CAR export (a directory or a .car zip).

Verified against a real JobDefinition XML sample (build 2023.2): element names for process
definitions, parameters, queue, type and script source.

Chains, queues, submit frames and job groups are parsed from the field names in Redwood's
published data model reference (2025.4), NOT from real export samples: element nesting and
enum encodings (name vs one-letter code) are assumed and every such object is reported in the
warnings as "unverified shape". Tests cover synthetic XML only. A submit frame does not name
its target; the binding lives on a JobGroup (JobDefinition + SubmitFrame + EndDate), so a
frame without a JobGroup is reported, not guessed.
"""
from __future__ import annotations

import zipfile
from collections import Counter
from pathlib import Path
from typing import Iterator

from defusedxml import ElementTree as ET  # CAR files come from customers: no entity expansion

from .model import Call, Chain, Model, Param, ProcessDef, Queue, Schedule, Step, parse_return_codes

# Job definition type (last segment of the type path) -> model type.
_TYPE_MAP = {"redwoodscript": "redwoodscript", "shell": "os", "sh": "os", "ksh": "os", "bash": "os", "python": "python"}
_DIRECTION = {"in": "in", "out": "out", "inout": "in_out", "in_out": "in_out"}


def _iter_xml(path: Path) -> Iterator[tuple[str, bytes]]:
    if path.is_dir():
        for p in sorted(path.rglob("*.xml")):
            yield str(p.relative_to(path)), p.read_bytes()
    else:
        with zipfile.ZipFile(path) as z:
            for name in sorted(z.namelist()):
                if name.endswith(".xml"):
                    yield name, z.read(name)


def _text(el, tag: str) -> str:
    child = el.find(tag)
    if child is None:
        return ""
    return " ".join("".join(child.itertext()).split())


def _raw(el, tag: str) -> str:
    child = el.find(tag)
    return "" if child is None else (child.text or "")


def _path_name(el, tag: str) -> str:
    """Last segment of a reference like <DefaultQueue type="Queue" path="GLOBAL.System"/>."""
    child = el.find(tag)
    path = "" if child is None else child.get("path", "")
    return path.rsplit(".", 1)[-1] if path else ""


def _is_true(value: str) -> bool:
    return value.strip().lower() == "true"


def _param(el) -> Param:
    default = _raw(el, "DefaultExpression")
    return Param(
        name=_text(el, "Name"),
        direction=_DIRECTION.get(_text(el, "Direction").lower(), "in"),
        type=_text(el, "DataType") or "string",
        default=default if default != "" else None,
        mandatory=not _is_true(_text(el, "Nullable") or "true"),
    )


def _process(root, source: str) -> ProcessDef:
    type_name = _path_name(root, "JobDefinitionType")
    ptype = _TYPE_MAP.get(type_name.lower(), type_name.lower() or "other")
    script = root.find("Script")
    command = _raw(script, "Source").strip("\n") if script is not None else ""
    run_as = _text(script, "RunAsUser") if script is not None else ""

    notes: list[str] = []
    # Attributes that change behavior but have no converter mapping yet.
    codes: list[int] = []
    for tag, label in (("TimeWindow", "time window"), ("Resource", "resource lock"),
                       ("RestartCount", "restart count"), ("ReturncodeMapToCompleted", "return-code mapping"),
                       ("JobTimeZone", "time zone")):
        value = _text(root, tag)
        if value:
            if tag == "ReturncodeMapToCompleted":
                parsed = parse_return_codes(value)
                if parsed is not None:
                    codes = parsed
                    continue
            notes.append(f"{label} {value!r} is set but not converted")
    for w in root.findall("JobDefinitionWaitEvent"):
        ev = _path_name(w, "EventDefinition")
        if ev:
            notes.append(f"waits for event {ev!r} before it starts (AutoSubmit={_text(w, 'AutoSubmit') or 'Never'}"
                         + (", clears it" if _is_true(_text(w, "ClearsEvent")) else "") + "); not converted: model it as a sensor, an upstream asset or the EventAutomationComponent")
    if _is_true(_text(root, "HoldOnSubmit")):
        notes.append("held on submit; not converted")
    if _is_true(_text(root, "CriticalProcess")):
        notes.append("marked critical process; not converted")
    if root.find("JobChain") is not None or type_name.lower() in ("chain", "jobchain"):
        ptype = "chain"
        pass            # steps and calls are read by _chain
    for p in root.findall("JobDefinitionParameter"):
        if _text(p, "Direction").lower() != "out" and (_raw(p, "DefaultExpression").startswith("=") or "${" in _raw(p, "DefaultExpression")):
            notes.append(f"parameter {_text(p, 'Name')!r} default looks like an expression, not a literal")

    return ProcessDef(
        name=_text(root, "Name"),
        type=ptype,
        application=_path_name(root, "ParentApplication"),
        description=_text(root, "Description"),
        command=command,
        parameters=[_param(p) for p in root.findall("JobDefinitionParameter")],
        queue=_path_name(root, "DefaultQueue"),
        run_as=run_as,
        extraction_notes=notes + [f"source: {source}"], success_exit_codes=codes,
    )


_UNITS = {"d": "days", "days": "days", "h": "hours", "hours": "hours", "m": "minutes", "minutes": "minutes",
          "s": "seconds", "seconds": "seconds", "o": "months", "months": "months", "y": "years", "years": "years"}
_RESTART = {"r": "restart_step", "reststep": "restart_step", "restartstep": "restart_step",
            "d": "restart_step", "restartstepdelayed": "restart_step"}
_OTHER_ACTIONS = {"markcompleted": "mark_completed", "raiseerror": "raise_error", "gotostep": "goto_step"}   # seen in real exports
_UNIT_SECONDS = {"seconds": 1, "minutes": 60, "hours": 3600, "days": 86400}
_STATUS = {"e": "Error", "error": "Error", "k": "Killed", "killed": "Killed", "a": "Canceled", "canceled": "Canceled"}


def _int(el, tag: str) -> int | None:
    t = _text(el, tag)
    return int(t) if t.lstrip("-").isdigit() else None


def _ref(el, tag: str) -> str:
    return _path_name(el, tag)


def _precondition(el, tag: str) -> dict | None:
    pre = el.find(tag)
    if pre is None:
        return None
    d = {"time_window": _ref(pre, "TimeWindow"), "status": _text(pre, "TimeWindowStatus"), "time_zone": _ref(pre, "TimeZone"),
         "expression": _text(pre, "Expression")}
    d = {k: v for k, v in d.items() if v and not (k == "status" and v == "None" and not d["time_window"])}
    return d or None


def _chain(root, source: str, model: Model) -> tuple[Chain, list[str]]:
    """A chain definition. Steps and calls are read from DIRECT children only: a call can hold a whole inline definition (a
    System_Internal_Chain wrapper) whose own steps must not be mixed into this chain. Inline definitions are added to `model` as chains
    (flagged inline) or process definitions, and the call refers to them by name."""
    notes: list[str] = []
    jc = root.find("JobChain")
    chain = Chain(name=_text(root, "Name"), application=_path_name(root, "ParentApplication"),
                  description=_text(root, "Description"))
    chain.parameters = [_param(p) for p in root.findall("JobDefinitionParameter") if _text(p, "Direction").lower() != "out"]
    if root.find("JobFileSearch") is not None:
        notes.append("output file search (JobFileSearch) is set on the chain but not converted: it sets a job status when a pattern is found in the output")
    steps = sorted(jc.findall("JobChainStep") if jc is not None else [], key=lambda s: _int(s, "SequenceNumber") or 0)
    for st in steps:
        calls = []
        for c in sorted(st.findall("JobChainCall"), key=lambda c: _int(c, "SequenceNumber") or 0):
            jd = c.find("JobDefinition")
            if jd is None:
                continue
            if jd.get("path"):
                proc = jd.get("path", "").rsplit(".", 1)[-1]
            else:                                                       # a definition embedded in the call
                inner = jd
                proc = _text(inner, "Name")
                if proc and proc not in model.chains and proc not in model.processes:
                    if inner.find("JobChain") is not None:
                        sub, sub_notes = _chain(inner, source, model)
                        sub.inline = True
                        model.chains[sub.name] = sub
                        notes.extend(f"inline chain {sub.name}: {n}" for n in sub_notes)
                    else:
                        model.processes[proc] = _process(inner, source)
            if not proc:
                continue
            label = _text(c, "Description") or f"{_text(st, 'Name')}_{len(calls) + 1}"
            call = Call(name=label.lower(), process=proc)
            for p in c.findall("JobChainCallInExpressionParameter"):         # a literal (or expression) set on the call
                local = _ref(p, "LocalJobDefinitionParameter")
                if local:
                    call.params[local] = "".join(p.find("Expression").itertext()) if p.find("Expression") is not None else ""
            for p in c.findall("JobChainCallInReferenceParameter"):          # taken from a parameter of the chain that holds the call
                local, src = _ref(p, "LocalJobDefinitionParameter"), _ref(p, "SourceJobDefinitionParameter")
                if local and src:
                    call.param_refs[local] = src
            for w in c.findall("JobChainCallWaitEvent"):
                ev = _ref(w, "EventDefinition")
                if ev:
                    call.wait_events.append({"event": ev, "clears": _is_true(_text(w, "ClearsEvent"))})
            call.precondition = _precondition(c, "JobChainCallPrecondition")
            calls.append(call)
        step = Step(name=_text(st, "Name") or f"step_{len(chain.steps) + 1}", calls=calls)
        step.precondition = _precondition(st, "JobChainStepPrecondition")
        for h in sorted(st.findall("JobChainStepStatusHandler"), key=lambda h: _int(h, "SequenceNumber") or 0):
            raw = _text(h, "Action")
            action = _RESTART.get(raw.lower())
            status = _STATUS.get(_text(h, "Status").lower(), _text(h, "Status"))
            if action:
                delay = (_int(st, "RestartDelayAmount") or 0) * _UNIT_SECONDS.get(_text(st, "RestartDelayUnits").lower(), 60)
                step.status_handlers.append({"status": status, "action": action, "max": _int(st, "RestartCount") or 1,
                                             **({"delay_seconds": delay} if delay else {})})
            else:
                d = {"status": status, "action": _OTHER_ACTIONS.get(raw.lower(), raw.lower())}
                for k, v in (("message", _text(h, "Message")), ("return_code", _text(h, "ReturnCode")), ("goto_step", _ref(h, "GotoStepJobChainStep"))):
                    if v:
                        d[k] = v
                step.status_handlers.append(d)
        chain.steps.append(step)
    if jc is not None and _text(jc, "RestartCount"):
        notes.append("chain-level restart count is not converted")
    chain.notes = notes
    return chain, notes


def _frame_elements(frame) -> list[dict]:
    out = []
    for el in frame.iter("SubmitFrameElement"):
        unit = _UNITS.get(_text(el, "Unit").lower())
        every = _int(el, "EveryNumberOf") or 1
        start = _int(el, "StartTime")           # assumed: milliseconds since midnight (unverified)
        at = f"{start // 3600000:02d}:{start // 60000 % 60:02d}" if start is not None else None
        tw = _ref(el, "DuringTimeWindow")
        if unit == "days" and every == 1 and at:
            d = {"kind": "daily", "at": at}
        elif unit in ("minutes", "hours"):
            d = {"kind": "interval", "every": every, "unit": unit}
        else:
            d = {"kind": "unsupported", "raw": f"unit={unit} every={every} start={at}"}
        if tw:
            d["time_window_unresolved"] = tw
        out.append(d)
    return out


def load_car(path: str | Path) -> tuple[Model, list[str]]:
    """Return (model, warnings). Warnings describe objects that were present but not converted."""
    model = Model()
    skipped: Counter[str] = Counter()
    unverified: Counter[str] = Counter()
    frames: dict[str, tuple] = {}
    groups: list[tuple] = []
    for name, data in _iter_xml(Path(path)):
        if name.replace("\\", "/").startswith("META-INF/"):
            continue
        root = ET.fromstring(data)
        if root.tag == "JobDefinition":
            proc = _process(root, name)
            if proc.type == "chain":
                chain, _n = _chain(root, name, model)
                chain.notes += [n for n in proc.extraction_notes if not n.startswith("source: ")]      # TimeWindow, restart count, return codes on the chain itself
                model.chains[chain.name] = chain
                continue
            key = proc.name if proc.name not in model.processes else f"{_path_name(root, 'Partition') or 'X'}.{proc.name}"
            model.processes[key] = proc
        elif root.tag == "EventDefinition":
            model.events[_text(root, "Name")] = _text(root, "Description")
        elif root.tag == "Queue":
            size = _int(root, "ExecutionSize")
            qn = _text(root, "Name")
            model.queues[qn] = Queue(name=qn, max_concurrency=size or None)
            unverified["Queue"] += 1
        elif root.tag == "SubmitFrame":
            frames[_text(root, "Name")] = (_frame_elements(root), _ref(root, "EndDateTimeZone"), _text(root, "EndDate"))
            unverified["SubmitFrame"] += 1
        elif root.tag == "JobGroup":
            groups.append((_ref(root, "JobDefinition"), _ref(root, "SubmitFrame"), _text(root, "Name")))
            unverified["JobGroup"] += 1
        else:
            skipped[root.tag] += 1

    for proc in model.processes.values():
        if proc.queue and proc.queue not in model.queues:
            model.queues[proc.queue] = Queue(name=proc.queue)  # limits live in the Queue object, which is not parsed yet

    bound: set[str] = set()
    for target, frame, gname in groups:
        if frame in frames and target:
            els, tz, end = frames[frame]
            model.schedules.append(Schedule(name=gname or frame, target=target, elements=els, end=end))
            bound.add(frame)
    warnings = [f"{n} {tag} object(s) not parsed (no verified schema yet)" for tag, n in sorted(skipped.items())]
    warnings += [f"{n} {tag} object(s) parsed from the data model reference, unverified shape" for tag, n in sorted(unverified.items())]
    warnings += [f"submit frame {f!r} has no JobGroup binding it to a process; not converted" for f in frames if f not in bound]
    if not model.schedules and not frames:
        warnings.append("no submit frames/job groups in this export; if schedules matter, export them or supply them separately")
    return model, warnings
