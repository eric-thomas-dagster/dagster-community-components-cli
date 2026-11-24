"""Extract a Model from JSON returned by the RunMyJobs Inbound REST Extension (swagger v1.1.0.0).

Shapes come from the published swagger.yml (JobDefinition with embedded JobChain/JobChainSteps/
JobChainCalls/status handlers/Script, Queue, EventDefinition), so field names and enum names are
exact. Still untested against a live server. The extension exposes NO endpoints for SubmitFrame,
JobGroup or TimeWindow, so schedules and time-window definitions cannot come from this source:
use a CAR export for them. Input: a directory of JSON files (as written by `fetch-ext`) or one file;
each file is either a list-response {"results": [...]} or a single object.
"""
from __future__ import annotations

import json
from pathlib import Path

from .model import Call, Chain, Model, Param, ProcessDef, Queue, Step, parse_return_codes

_DIR = {"in": "in", "out": "out", "inout": "in_out"}
_TYPES = {"redwoodscript": "redwoodscript", "shell": "os", "sh": "os", "ksh": "os", "bash": "os", "python": "python"}
_RESTART_ACTIONS = {"RestartStep", "RestartStepDelayed", "RestartStepMaster", "RestartStepDelayedMaster"}


def _key(ref) -> str:
    """'Queue:GLOBAL.Agentless_Queue' -> 'Agentless_Queue' (name part of a business key)."""
    if not ref:
        return ""
    return str(ref).split(":", 1)[-1].rsplit(".", 1)[-1]


def _chain(jd: dict, warnings: list[str]) -> Chain:
    name = jd.get("Name", "")
    jc = jd["JobChain"]
    chain = Chain(name=name, application=_key(jd.get("ParentApplication")), description=jd.get("Description", "") or "")
    for st in sorted(jc.get("JobChainSteps") or [], key=lambda s: s.get("SequenceNumber") or 0):
        calls = [Call(name=f"{st.get('Name', 'step')}_{i}".lower().replace(" ", "_"), process=_key(c.get("JobDefinition")))
                 for i, c in enumerate(sorted(st.get("JobChainCalls") or [], key=lambda c: c.get("SequenceNumber") or 0), 1)
                 if c.get("JobDefinition")]
        step = Step(name=st.get("Name") or f"step_{len(chain.steps) + 1}", calls=calls)
        pre = st.get("JobChainStepPrecondition") or {}
        if pre.get("Expression") or pre.get("TimeWindow"):
            step.precondition = {k: v for k, v in (("time_window", _key(pre.get("TimeWindow"))),
                                                   ("expression", pre.get("Expression"))) if v}
        for h in sorted(st.get("JobChainStepStatusHandlers") or [], key=lambda h: h.get("SequenceNumber") or 0):
            act = h.get("Action", "")
            if act in _RESTART_ACTIONS:
                step.status_handlers.append({"status": h.get("Status"), "action": "restart_step", "max": st.get("RestartCount") or 1})
            else:
                step.status_handlers.append({"status": h.get("Status"), "action": act})
        for c in st.get("JobChainCalls") or []:
            for field_, what in (("JobChainCallWaitEvents", "wait events"), ("JobChainCallPrecondition", "call precondition")):
                if c.get(field_):
                    warnings.append(f"chain {name} step {step.name}: {what} not converted")
        chain.steps.append(step)
    if jc.get("RestartCount"):
        warnings.append(f"chain {name}: chain-level restart count not converted")
    return chain


def _process(jd: dict) -> ProcessDef:
    tname = _key(jd.get("JobDefinitionType")).lower()
    script = jd.get("Script") or {}
    notes = []
    codes: list[int] = []
    for f, label in (("TimeWindow", "time window"), ("Resource", "resource"), ("RestartCount", "restart count"),
                     ("ReturncodeMapToCompleted", "return-code mapping")):
        if jd.get(f):
            if f == "ReturncodeMapToCompleted":
                parsed = parse_return_codes(jd[f])
                if parsed is not None:
                    codes = parsed
                    continue
            notes.append(f"{label} {jd[f]!r} is set but not converted")
    for f, label in (("JobDefinitionJobLocks", "job locks"), ("JobDefinitionWaitEvents", "wait events"),
                     ("JobDefinitionRaiseEvents", "raised events"), ("JobDefinitionRuntimeLimits", "runtime limits")):
        if jd.get(f):
            notes.append(f"{label} present; not converted")
    if jd.get("HoldOnSubmit"):
        notes.append("held on submit; not converted")
    params = [Param(name=p.get("Name", ""), direction=_DIR.get((p.get("Direction") or "In").lower(), "in"),
                    type=p.get("DataType") or "string", default=p.get("DefaultExpression") or None,
                    mandatory=p.get("Nullable") is False) for p in jd.get("JobDefinitionParameters") or []]
    return ProcessDef(name=jd.get("Name", ""), type=_TYPES.get(tname, tname or "other"),
                      application=_key(jd.get("ParentApplication")), description=jd.get("Description", "") or "",
                      command=(script.get("Source") or "").strip("\n"), parameters=params,
                      queue=_key(jd.get("DefaultQueue")), run_as=script.get("RunAsUser", "") or "",
                      extraction_notes=notes, success_exit_codes=codes)


def _objects(path: Path):
    files = sorted(path.glob("*.json")) if path.is_dir() else [path]
    for f in files:
        data = json.loads(f.read_text())
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            yield f.stem.split("__")[0], data["results"]
        else:
            yield f.stem.split("__")[0], [data]


def load_rest(path: str | Path) -> tuple[Model, list[str]]:
    model, warnings = Model(), []
    events = 0
    for kind, objs in _objects(Path(path)):
        if len(objs) >= 500:
            warnings.append(f"{kind}: list returned 500 objects (the API maximum); more may exist, so narrow with a filter")
        for o in objs:
            if kind == "JobDefinition" or "JobDefinitionType" in o or "JobChain" in o:
                if o.get("JobChain"):
                    ch = _chain(o, warnings)
                    model.chains[ch.name] = ch
                else:
                    p = _process(o)
                    model.processes[p.name] = p
            elif kind == "Queue":
                model.queues[o.get("Name", "")] = Queue(name=o.get("Name", ""), max_concurrency=o.get("ExecutionSize"))
                if o.get("Held") or o.get("TimeWindow"):
                    warnings.append(f"queue {o.get('Name')}: held/time window not converted")
            elif kind == "EventDefinition":
                events += 1
    if events:
        warnings.append(f"{events} EventDefinition object(s) read but not converted yet")
    warnings.append("schedules (submit frames) and time windows are not exposed by the REST extension; use a CAR export for them")
    return model, warnings
