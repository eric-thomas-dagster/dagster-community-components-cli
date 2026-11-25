"""A small, framework-agnostic record-graph runtime: stages, lanes, batches, offsets and an
error policy, with nothing StreamSets-specific in it. Extracted out of `runtime.py` (which has
one of these per generated migration project) so the engine itself has one tested source of
truth instead of drifting independently inside every copy. Pure Python, no `dagster` import
anywhere in this module -- it's usable and testable standalone; `runtime.py` (or any other
record-processing system) supplies the domain-specific stage factories and wires them to
Dagster.

Model: a pipeline is a graph of `Stage` nodes connected by named lanes. Origins (`gen` set)
produce batches of `Record`s; processors and destinations (`fn` set) consume a batch and route
the result by lane. A `Run` drives one materialization: for each origin, pull its batches
through the graph in topological order until the origin is exhausted, then deliver a
no-more-data `events` record (if the stage declares one) so downstream stages can react to end
of input.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field


class UnimplementedStageError(Exception):
    """A stage that was never implemented was reached while running the graph. Raised instead of
    silently skipping it, so a translation gap can never be mistaken for a successful run."""


class ErrorPolicy:
    """Named values for `Stage.on_error` -- plain string sentinels (any string works; these just
    give the two the engine actually understands a name). `CONTINUE`: a bad record is counted as
    an error and dropped; the run keeps going. `STOP`: the first bad record aborts the run."""
    CONTINUE = "TO_ERROR"
    STOP = "STOP_PIPELINE"


@dataclass
class Record:
    """A value tree plus string attributes -- the unit that moves through the graph in batches."""
    value: dict = field(default_factory=dict)
    attrs: dict = field(default_factory=dict)
    event: str | None = None

    @property
    def attributes(self) -> dict:
        return self.attrs

    def copy(self) -> "Record":
        return Record(json.loads(json.dumps(self.value, default=str)), dict(self.attrs), self.event)


# ---- field paths: "/a/b", "/a[0]/b", "/" -----------------------------------------------------
def _parts(path: str) -> list:
    out = []
    for seg in [x for x in path.strip().split("/") if x != ""]:
        seg = seg.strip("'\"")
        m = re.match(r"^(.*?)((?:\[\d+\])+)$", seg)
        if m and m.group(1):
            out.append(m.group(1))
            out += [int(i) for i in re.findall(r"\[(\d+)\]", m.group(2))]
        else:
            out.append(seg)
    return out


def get_path(value, path: str, default=None):
    cur = value
    for p in _parts(path):
        try:
            cur = cur[p]
        except (KeyError, IndexError, TypeError):
            return default
    return cur


def set_path(value: dict, path: str, v) -> None:
    ps = _parts(path)
    if not ps:
        if isinstance(v, dict):
            value.clear()
            value.update(v)
        return
    cur = value
    for p in ps[:-1]:
        cur = cur.setdefault(p, {}) if isinstance(cur, dict) else cur[p]
    cur[ps[-1]] = v


def del_path(value: dict, path: str) -> bool:
    ps = _parts(path)
    cur = value
    for p in ps[:-1]:
        try:
            cur = cur[p]
        except (KeyError, IndexError, TypeError):
            return False
    try:
        del cur[ps[-1]]
        return True
    except (KeyError, IndexError, TypeError):
        return False


def truthy(x) -> bool:
    return bool(x) if not isinstance(x, str) else x.lower() == "true"


# ---- engine -----------------------------------------------------------------------------------
class Stage:
    """A node: `fn(records) -> {lane: [records]}` for processors/destinations; origins set `gen`
    (a generator of `{lane: [records]}` batches) instead of `fn`."""

    def __init__(self, instance, fn=None, gen=None, inputs=(), outputs=(), events=(), on_error=ErrorPolicy.CONTINUE, pre=(), req=()):
        self.instance, self.fn, self.gen, self.req = instance, fn, gen, list(req)
        self.inputs, self.outputs, self.events = list(inputs), list(outputs), list(events)
        self.on_error, self.pre = on_error, list(pre)


def each(stage_holder: dict, run: "Run", fn):
    """Wraps `fn(record) -> record|list|None` to apply it to every record in a batch, routing any
    exception to the stage's error policy instead of failing the whole batch. `stage_holder` is a
    one-item dict a caller fills with `{"st": <the Stage>}` after constructing it (the stage
    needs to exist before this closure can reference it, and vice versa)."""
    def go(recs):
        out = []
        for r in recs:
            try:
                res = fn(r)
            except Exception as e:                    # noqa: BLE001 - a bad record follows the stage's on_error policy
                run._err(stage_holder["st"], r, f"{type(e).__name__}: {e}")
                continue
            if res is None:
                continue
            out.extend(res if isinstance(res, list) else [res])
        return out
    return go


def _topo(stages: list[Stage]) -> list[Stage]:
    """Orders stages so every stage runs after everything that feeds one of its input lanes."""
    prod = {ln: s.instance for s in stages for ln in [*s.outputs, *s.events]}
    by = {s.instance: s for s in stages}
    out, seen = [], set()

    def visit(s: Stage):
        if s.instance in seen:
            return
        seen.add(s.instance)
        for ln in s.inputs:
            if ln in prod:
                visit(by[prod[ln]])
        out.append(s)
    for s in stages:
        visit(s)
    return out


class Run:
    """Drives one materialization of a stage graph. Build it, `.add(stage)` each `Stage`, then
    call `.execute()`.

    `context`: whatever the caller wants `.log()` to use (anything with a `.log.info(msg)`, or
    None to print). `name`/`pid`: free-form identifiers available to stage factories for logging
    or EL-style self-reference; the engine itself never inspects them. `C`: the caller's resolved
    config/constants dict, handed to stage factories by whatever builds the graph -- the engine
    doesn't read it either, it's just carried on the Run for convenience. `offsets`: the offsets
    a PRIOR run of this same pipeline produced (see `offsets_in`/`offsets_out` below); `dry`: when
    true, stage factories that check `run.dry` should read nothing and skip writes, logging what
    they would have done instead (the engine itself doesn't enforce this -- each origin/
    destination stage factory opts in). `unimplemented_error`/`unimplemented_prefix`: the
    exception type and message prefix raised when the graph includes a stage that was never
    implemented (see `stub()` usage) -- override these to fold the engine's own vocabulary into a
    caller's domain-specific one without subclassing `Run.execute()`.
    """

    def __init__(self, context, name: str, C: dict, offsets: dict | None = None, pid: str = "", *,
                 dry: bool = False, unimplemented_error=UnimplementedStageError, unimplemented_prefix: str = ""):
        self.ctx, self.name, self.C, self.pid = context, name, C, pid
        self.offsets_in, self.offsets_out = dict(offsets or {}), dict(offsets or {})
        self.stages: list[Stage] = []
        self.counts = {"records_in": {}, "records_out": {}, "errors": {}}
        self.dry = dry
        self.stubs: list[tuple[str, str]] = []
        self.closers: list = []
        self.on_success: list = []
        self._unimplemented_error = unimplemented_error
        self._unimplemented_prefix = unimplemented_prefix

    def add(self, stage: Stage) -> None:
        self.stages.append(stage)

    def log(self, msg: str) -> None:
        (self.ctx.log.info if self.ctx is not None else print)(msg)

    def stub(self, inst: str, why: str, *, inputs=(), outputs=(), events=(), is_origin=False, pre=()):
        """Registers a stage that was never implemented. It keeps its place in the graph (so
        anything wired to its lanes still topologically sorts), and the run fails -- naming every
        such stage -- before any stage reads or writes anything, rather than silently treating a
        gap as success."""
        self.stubs.append((inst, why))
        self.add(Stage(inst, (lambda recs: {}), gen=(lambda: iter(())) if is_origin else None, inputs=inputs, outputs=outputs, events=events, pre=pre))

    def _precond(self, st: Stage, recs: list[Record]) -> list[Record]:
        if not st.pre and not st.req:
            return recs
        keep = []
        for r in recs:
            if st.req and any(get_path(r.value, f, _MISSING) is _MISSING for f in st.req):
                self._err(st, r, "required field missing")
            elif all(truthy(p(r)) for p in st.pre):
                keep.append(r)
            else:
                self._err(st, r, "stageRecordPreconditions not met", drop_ok=True)
        return keep

    def _err(self, st: Stage, rec: Record, why: str, drop_ok: bool = False) -> None:
        self.counts["errors"][st.instance] = self.counts["errors"].get(st.instance, 0) + 1
        if st.on_error == ErrorPolicy.STOP:
            raise RuntimeError(f"{st.instance}: {why}")

    def _deliver(self, lanes: dict, pending: dict) -> None:
        for lane, recs in lanes.items():
            if recs:
                pending.setdefault(lane, []).extend(recs)

    def _push(self, st_index: int, pending: dict, order: list[Stage]) -> None:
        for st in order[st_index:]:
            if st.gen is not None:
                continue
            recs = [r for ln in st.inputs for r in pending.pop(ln, [])]
            if not recs:
                continue
            recs = self._precond(st, recs)
            if not recs:
                continue
            self.counts["records_in"][st.instance] = self.counts["records_in"].get(st.instance, 0) + len(recs)
            out = st.fn(recs) or {}
            for ln, rs in out.items():
                self.counts["records_out"][st.instance] = self.counts["records_out"].get(st.instance, 0) + len(rs)
            self._deliver(out, pending)

    def execute(self) -> dict:
        try:
            stats = self._execute()
            for hook in self.on_success:          # e.g. commit a source's read offsets, only once the whole run succeeded
                hook()
            return stats
        finally:
            for c in self.closers:
                c()

    def _execute(self) -> dict:
        if self.stubs:
            # A stage that was never implemented means this graph is not the real pipeline: fail
            # before touching any data, never "succeed".
            raise self._unimplemented_error(self._unimplemented_prefix + "; ".join(f"{i}: {why}" for i, why in self.stubs))
        order = _topo(self.stages)
        origins = [s for s in order if s.gen is not None]
        for o in origins:
            pending: dict = {}
            for lanes in o.gen():
                self.counts["records_out"][o.instance] = self.counts["records_out"].get(o.instance, 0) + sum(len(v) for ln, v in lanes.items() if ln in o.outputs)
                self._deliver(lanes, pending)
                self._push(0, pending, order)
            # origin exhausted: the batch pipeline's "no-more-data" event
            if o.events:
                self._deliver({o.events[0]: [Record({}, {"sdc.event.type": "no-more-data"}, "no-more-data")]}, pending)
                self._push(0, pending, order)
        return {"records_in": self.counts["records_in"], "records_out": self.counts["records_out"], "error_records": self.counts["errors"]}


Pipeline = Run  # a more conventional name for consumers outside this migration tool


_MISSING = object()
