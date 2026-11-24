"""Load the intermediate YAML model, validating references."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

import re

from .model import RUN_WINDOW_RE, Call, FileEvent, Chain, Model, Param, ProcessDef, Queue, Schedule, Step


class ModelError(ValueError):
    pass


def _params(raw: list[dict[str, Any]] | None) -> list[Param]:
    return [Param(**p) for p in raw or []]


def load_model(path: str | Path) -> Model:
    data = yaml.safe_load(Path(path).read_text()) or {}
    m = Model()

    for q in data.get("queues", []):
        m.queues[q["name"]] = Queue(**q)

    for p in data.get("process_definitions", []):
        p = dict(p)
        p["parameters"] = _params(p.get("parameters"))
        m.processes[p["name"]] = ProcessDef(**p)

    for c in data.get("chains", []):
        steps = []
        for s in c.get("steps", []):
            calls = [Call(**call) for call in s.get("calls", [])]
            steps.append(Step(
                name=s["name"], calls=calls,
                precondition=s.get("precondition"),
                status_handlers=s.get("status_handlers", []),
            ))
        m.chains[c["name"]] = Chain(
            name=c["name"], application=c.get("application", ""),
            description=c.get("description", ""), steps=steps,
            precondition=c.get("precondition"), run_window=c.get("run_window", ""), parameters=_params(c.get("parameters")),
            inline=bool(c.get("inline", False)), notes=list(c.get("notes") or []),
        )

    for s in data.get("schedules", []):
        m.schedules.append(Schedule(**s))

    m.events = {str(k): str(v or "") for k, v in (data.get("events") or {}).items()}
    for fe in data.get("file_events", []):
        try:
            m.file_events.append(FileEvent(**fe))
        except TypeError as e:
            raise ModelError(f"file event {fe.get('name', fe)!r}: {e}") from e

    _validate(m)
    return m


def _validate(m: Model) -> None:
    errors: list[str] = []
    for c in m.chains.values():
        for s in c.steps:
            pass            # a call to a definition that is not in the export is not an error here: convert makes it a loud stub (or a no-op for a built-in)
    for owner, w in [(f"process {p.name!r}", p.run_window) for p in m.processes.values()] + [(f"chain {c.name!r}", c.run_window) for c in m.chains.values()]:
        if w and not re.match(RUN_WINDOW_RE, w):
            errors.append(f"{owner}: run_window {w!r} must look like '22:00-23:59 America/New_York mon,tue'")
    for p in m.processes.values():
        if any((not isinstance(c, int)) or c < 0 or c > 255 for c in p.success_exit_codes):
            errors.append(f"process {p.name!r}: success_exit_codes must be integers 0-255")
    names = set()
    for fe in m.file_events:
        if fe.target not in m.processes and fe.target not in m.chains:
            errors.append(f"file event {fe.name!r}: unknown target {fe.target!r}")
        if not fe.directory:
            errors.append(f"file event {fe.name!r}: directory is required")
        if fe.name in names:
            errors.append(f"file event {fe.name!r}: duplicate name")
        names.add(fe.name)
        for f_ in ("poll_seconds", "min_size_bytes", "min_age_seconds"):
            v = getattr(fe, f_)
            if v is not None and (not isinstance(v, int) or v < 0):
                errors.append(f"file event {fe.name!r}: {f_} must be a non-negative integer")
    for s in m.schedules:
        if s.target not in m.processes and s.target not in m.chains:
            errors.append(f"schedule {s.name!r}: unknown target {s.target!r}")
    if errors:
        raise ModelError("\n".join(errors))
