"""Normalized intermediate model of a RunMyJobs installation.

Everything downstream (mapper, emitter, report) works on this model, never on
raw export files. Extractors (CAR/XML, REST API, hand-written YAML) all produce it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Param:
    name: str
    direction: str = "in"          # in | out | in_out
    type: str = "string"
    default: Any = None
    mandatory: bool = False


@dataclass
class ProcessDef:
    name: str
    type: str = "os"               # os | sql | redwoodscript | sap | other
    application: str = ""
    description: str = ""
    command: str = ""              # shell command / SQL text / RedwoodScript source
    parameters: list[Param] = field(default_factory=list)
    queue: str = ""
    run_as: str = ""
    max_starts: int = 1            # RunMyJobs "Maximum Starts"; retries = max_starts - 1
    timeout_seconds: int | None = None
    time_zone: str = ""
    connection: str = ""           # logical DB connection name for SQL steps
    outputs: list[str] = field(default_factory=list)  # data the process produces (tables, files)
    extraction_notes: list[str] = field(default_factory=list)  # attributes seen by an extractor but not converted
    success_exit_codes: list[int] = field(default_factory=list)  # extra exit codes counted as success (RunMyJobs return-code mapping to Completed)
    run_window: str = ""           # "HH:MM-HH:MM tz [day,day]": outside it the run is skipped, not failed (a RunMyJobs time window)


@dataclass
class Call:
    name: str
    process: str
    params: dict[str, Any] = field(default_factory=dict)          # parameter -> literal value set on the call
    param_refs: dict[str, str] = field(default_factory=dict)      # parameter -> the CALLER's parameter it takes its value from
    wait_events: list[dict[str, Any]] = field(default_factory=list)   # [{"event": name, "clears": bool}]: the call waits for the event
    precondition: dict[str, Any] | None = None                    # {time_window, status (IsOpen/IsClosed), time_zone, expression}: the call runs only if it holds


@dataclass
class Step:
    name: str
    calls: list[Call] = field(default_factory=list)
    precondition: dict[str, Any] | None = None       # {time_window: ..., expression: ...}
    status_handlers: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Chain:
    name: str
    application: str = ""
    description: str = ""
    steps: list[Step] = field(default_factory=list)
    precondition: dict[str, Any] | None = None
    parameters: list[Param] = field(default_factory=list)
    inline: bool = False           # defined inside a call of another chain (a System_Internal_Chain wrapper): it only runs as part of that chain
    notes: list[str] = field(default_factory=list)   # attributes of the chain the extractor saw but did not convert
    run_window: str = ""            # "HH:MM-HH:MM tz [day,day]", from a time window the extractor could resolve or the hand-written model


@dataclass
class Schedule:
    """A submit frame (recurrence) attached to a process or chain."""
    name: str
    target: str                    # process or chain name
    elements: list[dict[str, Any]] = field(default_factory=list)
    time_zone: str = "UTC"
    end: str = ""


@dataclass
class Queue:
    name: str
    max_concurrency: int | None = None
    process_server: str = ""


@dataclass
class FileEvent:
    """A RunMyJobs file event: start `target` (a chain or process) when a file arrives."""
    name: str
    target: str
    directory: str
    pattern: str = "*"             # glob on the file name
    poll_seconds: int = 30
    min_size_bytes: int | None = None
    min_age_seconds: int | None = None   # the file must not have been modified for this long
    stable: bool = False           # the "file in use" check: skip a file whose size changed since the last poll
    recursive: bool = False
    one_run_per_poll: bool = False  # one run per poll that found files (a target that processes the whole folder), not one per file


@dataclass
class Model:
    processes: dict[str, ProcessDef] = field(default_factory=dict)
    chains: dict[str, Chain] = field(default_factory=dict)
    schedules: list[Schedule] = field(default_factory=list)
    queues: dict[str, Queue] = field(default_factory=dict)
    file_events: list[FileEvent] = field(default_factory=list)
    events: dict[str, str] = field(default_factory=dict)         # RunMyJobs event definitions in the export: name -> description


RUN_WINDOW_RE = r"^\d{1,2}:\d{2}-\d{1,2}:\d{2}( [A-Za-z_]+/[A-Za-z_]+| UTC)?( (mon|tue|wed|thu|fri|sat|sun)(,(mon|tue|wed|thu|fri|sat|sun))*)?$"


def parse_return_codes(value) -> list[int] | None:
    """Exit codes from a return-code mapping: 3, "0,3", "0-3", [0, 3]. None when the text is not in a shape we know (the caller keeps a note).
    Zero is dropped (always success). Unverified against a real export."""
    import re
    if value is None or value == "":
        return []
    parts = value if isinstance(value, (list, tuple)) else re.split(r"[,\s;]+", str(value).strip())
    codes: list[int] = []
    for p in parts:
        p = str(p).strip()
        if not p:
            continue
        if re.fullmatch(r"-?\d+", p):
            codes.append(int(p))
        elif re.fullmatch(r"\d+\s*-\s*\d+", p):
            lo, hi = (int(x) for x in re.split(r"\s*-\s*", p))
            if hi - lo > 255:
                return None
            codes.extend(range(lo, hi + 1))
        else:
            return None
    return sorted(set(c for c in codes if c != 0))
