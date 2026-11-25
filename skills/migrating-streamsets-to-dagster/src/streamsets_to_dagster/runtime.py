"""Runtime for generated StreamSets migrations (copied into each generated project as `runtime.py`).

StreamSets moves *records* (a value tree plus string attributes) through a graph of stages in batches. This module keeps that model so a
translated pipeline behaves the way the original stage graph does: origins emit batches (and events), processors and destinations consume
them, a Stream Selector fans out by lane. A Dagster materialization runs the graph until every origin is exhausted ("no-more-data").

Environment:
  SDC_DRY_RUN=1                 origins read nothing and destinations/executors log instead of writing (used by `validate --execute`)
  SDC_PARAM_<NAME>              overrides runtime parameter NAME (the pipeline's constants)
  SDC_SECRET_<NAME>             value of a credential-like runtime parameter (never stored in the project)
  SDC_SQLALCHEMY_URL_<STAGE>    full SQLAlchemy URL for a JDBC stage, replacing the one derived from the stage's JDBC URL
"""
from __future__ import annotations

import copy
import datetime as _dt
import fnmatch
import json
import os
import re
import uuid as _uuid
from dataclasses import dataclass, field


class SdcCaveat(Exception):
    """A stage that was not translated: raised when the stage receives data, so a gap can never pass as success."""


def load_offsets(context, asset_key) -> dict:
    """Offsets stored by the previous materialization (SDC keeps them in the pipeline's offset file)."""
    try:
        ev = context.instance.get_latest_materialization_event(asset_key)
        md = ev.asset_materialization.metadata if ev and ev.asset_materialization else {}
        return dict(md["sdc_offsets"].value) if "sdc_offsets" in md else {}
    except Exception:                                    # noqa: BLE001 - no instance or no history: start from the beginning
        return {}


def stage_secret(inst: str, name: str) -> str:
    """A credential literal that was removed from the export: SDC_SECRET_<STAGE>_<NAME>."""
    return os.environ.get(f"SDC_SECRET_{re.sub(r'[^A-Za-z0-9]', '_', inst).upper()}_{name}", "")


def dry_run() -> bool:
    return os.environ.get("SDC_DRY_RUN") == "1"


def constants(defaults: dict, secrets: list[str]) -> dict:
    out = dict(defaults)
    for k in list(out):
        out[k] = os.environ.get(f"SDC_PARAM_{k}", out[k])
    for k in secrets:
        v = os.environ.get(f"SDC_SECRET_{k}")
        if v is None and not dry_run():
            v = ""      # resolved lazily: a stage that needs it fails with a clear message when it connects
        out[k] = v or ""
    return out


@dataclass
class Record:
    value: dict = field(default_factory=dict)
    attrs: dict = field(default_factory=dict)
    event: str | None = None

    @property
    def attributes(self) -> dict:                 # name used by StreamSets scripts
        return self.attrs

    def copy(self) -> "Record":
        import copy
        return Record(copy.deepcopy(self.value), dict(self.attrs), self.event)


# ---- field paths: "/a/b", "/a[0]/b", "/" ------------------------------------------------------------------------------------------------------
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
    if isinstance(cur, dict):
        cur[ps[-1]] = v
    else:
        cur[ps[-1]] = v


def del_path(value: dict, path: str) -> bool:
    ps = _parts(path)
    if not ps:
        value.clear()
        return True
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


def _truthy(x) -> bool:
    return bool(x) if not isinstance(x, str) else x.lower() == "true"


class fx:
    """Implementations of the supported EL functions. Names match `el.FUNCS`."""

    @staticmethod
    def interp(parts):
        return "".join("" if p is None else str(p) for p in parts)

    @staticmethod
    def eq(a, b):
        if a is None or b is None:
            return a is b
        try:
            return float(a) == float(b) if not (isinstance(a, str) and isinstance(b, str)) else a == b
        except (TypeError, ValueError):
            return str(a) == str(b)

    @staticmethod
    def cmp(a, op, b):
        try:
            a, b = (float(a), float(b)) if not (isinstance(a, str) and isinstance(b, str)) else (a, b)
        except (TypeError, ValueError):
            a, b = str(a), str(b)
        return {"<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op]

    @staticmethod
    def arith(a, op, b):
        ints = all(isinstance(x, int) or (isinstance(x, str) and re.fullmatch(r"-?\d+", x.strip() or "x")) for x in (a, b))
        x, y = float(a or 0), float(b or 0)
        r = {"+": x + y, "-": x - y, "*": x * y, "/": x / y if y else float("inf"), "%": x % y if y else 0.0}[op]
        return int(r) if ints and op != "/" else r

    @staticmethod
    def empty(x):
        return x is None or x == "" or x == [] or x == {}

    @staticmethod
    def record_value(rec, path):
        return get_path(rec.value, path)

    @staticmethod
    def record_value_or(rec, path, default):
        v = get_path(rec.value, path)
        return default if v is None else v

    @staticmethod
    def record_attribute(rec, name):
        return rec.attrs.get(name)

    @staticmethod
    def record_attribute_or(rec, name, default):
        v = rec.attrs.get(name)
        return default if v is None else v

    @staticmethod
    def record_exists(rec, path):
        return get_path(rec.value, path, _MISSING) is not _MISSING

    @staticmethod
    def record_event_type(rec):
        return rec.event

    to_lower = staticmethod(lambda s: None if s is None else str(s).lower())
    to_upper = staticmethod(lambda s: None if s is None else str(s).upper())
    trim = staticmethod(lambda s: None if s is None else str(s).strip())
    length = staticmethod(lambda s: len(str(s or "")))
    concat = staticmethod(lambda a, b: f"{'' if a is None else a}{'' if b is None else b}")
    replace = staticmethod(lambda s, a, b: str(s).replace(a, b))
    contains = staticmethod(lambda s, sub: sub in str(s))
    starts_with = staticmethod(lambda s, p: str(s).startswith(p))
    ends_with = staticmethod(lambda s, p: str(s).endswith(p))
    index_of = staticmethod(lambda s, sub: str(s).find(sub))
    is_null_or_empty = staticmethod(lambda s: s is None or s == "")

    @staticmethod
    def substring(s, start, end=None):
        s = str(s)
        return s[int(start):] if end is None else s[int(start):int(end)]

    @staticmethod
    def truncate(s, n):
        return str(s)[:int(n)]

    @staticmethod
    def replace_all(s, pattern, repl):
        return re.sub(pattern, repl, str(s))

    @staticmethod
    def matches(s, pattern):
        return re.fullmatch(pattern, str(s)) is not None     # whole-string match, as Java's String.matches (mapping.md)

    @staticmethod
    def reg_ex_capture(s, pattern, group):
        m = re.fullmatch(pattern, str(s))       # StreamSets documents a whole-string match; verify against your version (mapping.md)
        return m.group(int(group)) if m else ""

    m_abs, m_ceil = staticmethod(abs), staticmethod(lambda x: int(-(-float(x) // 1)))
    m_floor, m_round = staticmethod(lambda x: int(float(x) // 1)), staticmethod(lambda x: int(round(float(x))))
    m_max, m_min = staticmethod(max), staticmethod(min)

    @staticmethod
    def time_now():
        return _dt.datetime.now(_dt.timezone.utc)

    yyyy = staticmethod(lambda: f"{_dt.datetime.now(_dt.timezone.utc):%Y}")
    mm = staticmethod(lambda: f"{_dt.datetime.now(_dt.timezone.utc):%m}")
    dd = staticmethod(lambda: f"{_dt.datetime.now(_dt.timezone.utc):%d}")
    hh = staticmethod(lambda: f"{_dt.datetime.now(_dt.timezone.utc):%H}")
    minute = staticmethod(lambda: f"{_dt.datetime.now(_dt.timezone.utc):%M}")
    ss = staticmethod(lambda: f"{_dt.datetime.now(_dt.timezone.utc):%S}")
    uuid = staticmethod(lambda: str(_uuid.uuid4()))
    PIPELINE: dict = {"id": "", "title": ""}

    @staticmethod
    def runtime_conf(name):
        """`runtime:conf('x')` reads Data Collector's configuration; here the environment variable SDC_RUNTIME_CONF_<X>."""
        key = "SDC_RUNTIME_CONF_" + re.sub(r"[^A-Za-z0-9]", "_", str(name)).upper()
        if key not in os.environ:
            raise KeyError(f"set {key} (runtime:conf({name!r}))")
        return os.environ[key]

    pipeline_id = staticmethod(lambda: fx.PIPELINE["id"])
    pipeline_title = staticmethod(lambda: fx.PIPELINE["title"])

    @staticmethod
    def date_time_to_ms(d):
        d = d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)
        return int(d.timestamp() * 1000)

    @staticmethod
    def ms_to_date_time(ms):
        return _dt.datetime.fromtimestamp(float(ms) / 1000, _dt.timezone.utc)

    @staticmethod
    def create_date_from_string(s, java_fmt):
        return parse_date(s, java_fmt)

    @staticmethod
    def create_date_from_string_tz(s, tz, java_fmt):
        import zoneinfo
        d = parse_date(s, java_fmt)
        return d if d.tzinfo else d.replace(tzinfo=zoneinfo.ZoneInfo(tz))

    @staticmethod
    def date_time_zone_offset(d, tz):
        import zoneinfo
        d = d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)
        return int(d.astimezone(zoneinfo.ZoneInfo(tz)).utcoffset().total_seconds() * 1000)

    @staticmethod
    def extract_string_from_date(d, java_fmt):
        return d.strftime(_strptime_format(java_fmt))
    empty_map = staticmethod(lambda: {})
    empty_list = staticmethod(lambda: [])

    file_name = staticmethod(lambda p: str(p).rstrip("/").rsplit("/", 1)[-1])
    file_parent_path = staticmethod(lambda p: str(p).rstrip("/").rsplit("/", 1)[0] if "/" in str(p).strip("/") else "/")
    file_remove_extension = staticmethod(lambda p: str(p).rsplit(".", 1)[0])

    @staticmethod
    def file_path_element(p, i):
        parts = [x for x in str(p).split("/") if x]
        return parts[int(i)]

    # Field Mapper context: `f` is a dict {name, value, path, type, index}
    f_name = staticmethod(lambda rec, f: f["name"])
    f_value = staticmethod(lambda rec, f: f["value"])
    f_path = staticmethod(lambda rec, f: f["path"])
    f_type = staticmethod(lambda rec, f: type(f["value"]).__name__)
    f_index = staticmethod(lambda rec, f: f.get("index", 0))
    f_parent_path = staticmethod(lambda rec, f: f["path"].rsplit("/", 1)[0] or "/")


_MISSING = object()


# ---- Java date patterns -> strptime --------------------------------------------------------------------------------------------------------------
_JAVA = [("yyyy", "%Y"), ("YYYY", "%Y"), ("SSS", "%f"), ("MMMM", "%B"), ("MMM", "%b"), ("MM", "%m"), ("dd", "%d"), ("HH", "%H"), ("mm", "%M"), ("ss", "%S"),
         ("EEE", "%a"), ("y", "%Y"), ("M", "%m"), ("d", "%d"), ("H", "%H"), ("m", "%M"), ("s", "%S"), ("Z", "%z"), ("z", "%Z"), ("X", "%z")]


def _strptime_format(java: str) -> str:
    out, i = "", 0
    while i < len(java):
        if java[i] == "'":
            j = java.index("'", i + 1)
            out += java[i + 1:j]
            i = j + 1
            continue
        for tok, py in _JAVA:
            if java.startswith(tok, i):
                out += py
                i += len(tok)
                break
        else:
            out += java[i].replace("%", "%%")
            i += 1
    return out


# StreamSets' named date formats (Field Type Converter)
NAMED_DATE_FORMATS = {"YYYY_MM_DD": "yyyy-MM-dd", "YYYY_MM_DD_HH_MM_SS": "yyyy-MM-dd HH:mm:ss", "YYYY_MM_DD_HH_MM_SS_SSS": "yyyy-MM-dd HH:mm:ss.SSS",
                      "DD_MM_YYYY": "dd-MM-yyyy", "MM_DD_YYYY": "MM-dd-yyyy"}


def parse_date(s, java: str):
    if isinstance(s, _dt.datetime):
        return s
    return _dt.datetime.strptime(str(s).strip(), _strptime_format(java))


# ---- engine --------------------------------------------------------------------------------------------------------------------------------
class Stage:
    """A node: `fn(records) -> {lane: [records]}` for processors/targets; origins set `gen` instead."""

    def __init__(self, instance, fn=None, gen=None, inputs=(), outputs=(), events=(), on_error="TO_ERROR", pre=(), req=()):
        self.instance, self.fn, self.gen, self.req = instance, fn, gen, list(req)
        self.inputs, self.outputs, self.events = list(inputs), list(outputs), list(events)
        self.on_error, self.pre = on_error, list(pre)


class Run:
    def __init__(self, context, name: str, C: dict, offsets: dict | None = None, pid: str = ""):
        self.ctx, self.name, self.C = context, name, C
        fx.PIPELINE = {"id": pid or name, "title": name}
        self.offsets_in, self.offsets_out = dict(offsets or {}), dict(offsets or {})
        self.stages: list[Stage] = []
        self.counts = {"records_in": {}, "records_out": {}, "errors": {}}
        self.dry = dry_run()
        self.stubs: list[tuple[str, str]] = []
        self.closers: list = []
        self.on_success: list = []

    def add(self, stage: Stage) -> None:
        self.stages.append(stage)

    def log(self, msg: str) -> None:
        (self.ctx.log.info if self.ctx is not None else print)(msg)

    def _precond(self, st: Stage, recs: list[Record]) -> list[Record]:
        if not st.pre and not st.req:
            return recs
        keep = []
        for r in recs:
            if st.req and any(get_path(r.value, f, _MISSING) is _MISSING for f in st.req):
                self._err(st, r, "required field missing")
            elif all(fx_truthy(p(r)) for p in st.pre):
                keep.append(r)
            else:
                self._err(st, r, "stageRecordPreconditions not met", drop_ok=True)
        return keep

    def _err(self, st: Stage, rec: Record, why: str, drop_ok: bool = False) -> None:
        self.counts["errors"][st.instance] = self.counts["errors"].get(st.instance, 0) + 1
        if st.on_error == "STOP_PIPELINE":
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
            for hook in self.on_success:          # e.g. commit Kafka offsets, only once the whole run succeeded
                hook()
            return stats
        finally:
            for c in self.closers:
                c()

    def _execute(self) -> dict:
        if self.stubs:
            # A stage that was not translated means this graph is not the original pipeline: fail before touching any data, never "succeed".
            raise SdcCaveat("SDC-CAVEAT: " + "; ".join(f"{i}: {why}" for i, why in self.stubs))
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


def fx_truthy(x) -> bool:
    return _truthy(x)


def _topo(stages: list[Stage]) -> list[Stage]:
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


# ---- stage factories ------------------------------------------------------------------------------------------------------------------------
def _each(st_holder: dict, run: Run, fn):
    """Apply fn(record)->record|list|None to every record, routing a failure per the stage's error policy."""
    def go(recs):
        out = []
        for r in recs:
            try:
                res = fn(r)
            except Exception as e:                    # noqa: BLE001 - a bad record follows stageOnRecordError
                run._err(st_holder["st"], r, f"{type(e).__name__}: {e}")
                continue
            if res is None:
                continue
            out.extend(res if isinstance(res, list) else [res])
        return out
    return go


def expression(run: Run, inst: str, fields, headers, *, inputs, outputs, on_error="TO_ERROR", pre=(), req=()):
    """Expression Evaluator: fields = [(path, fn)], headers = [(attribute, fn)]. Header expressions see the record before field changes (as in SDC)."""
    holder: dict = {}

    def one(r: Record):
        for name, fn in headers:
            r.attrs[name] = None if (v := fn(r)) is None else str(v)
        for path, fn in fields:
            set_path(r.value, path, fn(r))
        return r
    each = _each(holder, run, one)
    st = Stage(inst, lambda recs: {outputs[0]: each(recs)} if outputs else {}, inputs=inputs, outputs=outputs, on_error=on_error, pre=pre, req=req)
    holder["st"] = st
    run.add(st)


def field_filter(run: Run, inst: str, op: str, fields: list[str], *, inputs, outputs, on_error="TO_ERROR", pre=(), req=()):
    """Field Remover (stage FieldFilter): op is REMOVE | KEEP | REMOVE_NULL | REMOVE_EMPTY | REMOVE_NULL_EMPTY | REMOVE_CONSTANT."""
    def one(r: Record):
        if op == "REMOVE":
            for f in fields:
                del_path(r.value, f)
        elif op == "KEEP":
            kept = {}
            for f in fields:
                v = get_path(r.value, f, _MISSING)
                if v is not _MISSING:
                    set_path(kept, f, v)
            r.value = kept
        return r
    holder: dict = {}
    each = _each(holder, run, one)
    st = Stage(inst, lambda recs: {outputs[0]: each(recs)}, inputs=inputs, outputs=outputs, on_error=on_error, pre=pre, req=req)
    holder["st"] = st
    run.add(st)


def field_flattener(run: Run, inst: str, fields: list[str], sep: str, target: str, remove: bool, *, in_place: bool = False, inputs, outputs, on_error="TO_ERROR", pre=(), req=()):
    def flat(v, prefix):
        if isinstance(v, dict):
            for k, x in v.items():
                yield from flat(x, f"{prefix}{sep}{k}" if prefix else str(k))
        elif isinstance(v, list):
            for i, x in enumerate(v):
                yield from flat(x, f"{prefix}{sep}{i}" if prefix else str(i))      # list elements are numbered with the separator: l.0, l.1 (Data Collector 3.18.1)
        else:
            yield prefix, v

    def one(r: Record):
        for f in fields:
            sub = get_path(r.value, f, _MISSING)
            if sub is _MISSING:
                continue
            if in_place and f.strip("/") != "":
                set_path(r.value, f, dict(flat(sub, "")))      # flatten inside the field, in its place: o = {"k": 1, "d.deep": 2}
                continue
            if f.strip("/") == "":
                sub = copy.deepcopy(sub)
            if remove:
                del_path(r.value, f)
            base = "" if target in ("", "/") else f.strip("/").replace("/", sep)      # flattening to the root drops the parent's name (mapping.md: unverified)
            for k, x in flat(sub, base):
                if target in ("", "/"):
                    r.value[k] = x                       # keys such as `a.c[0]` are names, not paths
                else:
                    set_path(r.value, target.rstrip("/") + "/" + k, x)
        return r
    holder: dict = {}
    each = _each(holder, run, one)
    st = Stage(inst, lambda recs: {outputs[0]: each(recs)}, inputs=inputs, outputs=outputs, on_error=on_error, pre=pre, req=req)
    holder["st"] = st
    run.add(st)


def selector(run: Run, inst: str, preds, *, inputs, outputs, on_error="TO_ERROR", pre=(), req=()):
    """Stream Selector: preds = [(lane, fn)] in order; fn None marks the `default` lane. A record goes to every lane whose predicate is true."""
    def go(recs):
        out = {ln: [] for ln, _ in preds}
        for r in recs:
            hit = False
            for ln, fn in preds:
                if fn is not None and fx_truthy(fn(r)):
                    out[ln].append(r.copy() if hit else r)
                    hit = True
            if not hit:
                for ln, fn in preds:
                    if fn is None:
                        out[ln].append(r)
        return out
    run.add(Stage(inst, go, inputs=inputs, outputs=outputs, on_error=on_error, pre=pre, req=req))


def field_mapper(run: Run, inst: str, operate_on: str, mapping, cond, *, inputs, outputs, on_error="TO_ERROR", pre=(), req=()):
    """Field Mapper over FIELD_NAMES or FIELD_VALUES. `mapping(rec, f)` returns the new name or value; `cond(rec, f)` filters which fields change."""
    def walk(rec, node, path):
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                p = f"{path}/{k}"
                if isinstance(v, (dict, list)):
                    out[k if operate_on != "FIELD_NAMES" else _name(rec, k, v, p)] = walk(rec, v, p)
                elif operate_on == "FIELD_NAMES":
                    out[_name(rec, k, v, p)] = v
                else:
                    f = {"name": k, "value": v, "path": p}
                    out[k] = mapping(rec, f) if cond is None or fx_truthy(cond(rec, f)) else v
            return out
        if isinstance(node, list):
            return [walk(rec, x, f"{path}[{i}]") for i, x in enumerate(node)]
        return node

    def _name(rec, k, v, p):
        f = {"name": k, "value": v, "path": p}
        return mapping(rec, f) if cond is None or fx_truthy(cond(rec, f)) else k

    def one(r: Record):
        r.value = walk(r, r.value, "")
        return r
    holder: dict = {}
    each = _each(holder, run, one)
    st = Stage(inst, lambda recs: {outputs[0]: each(recs)}, inputs=inputs, outputs=outputs, on_error=on_error, pre=pre, req=req)
    holder["st"] = st
    run.add(st)


def null_target(run: Run, inst: str, *, inputs, on_error="TO_ERROR", pre=(), req=()):
    run.add(Stage(inst, lambda recs: {}, inputs=inputs, on_error=on_error, pre=pre, req=req))


def pipeline_finisher(run: Run, inst: str, *, inputs, on_error="TO_ERROR", pre=(), req=()):
    """Pipeline Finisher: in a batch run the pipeline ends when the origins are exhausted, so receiving the record changes nothing here."""
    run.add(Stage(inst, lambda recs: {}, inputs=inputs, on_error=on_error, pre=pre, req=req))


def stub(run: Run, inst: str, why: str, *, inputs, outputs=(), events=(), is_origin=False, pre=()):
    """A stage that was not translated. It keeps its place in the graph, and the run fails before reading anything, naming the reason."""
    run.stubs.append((inst, why))
    run.add(Stage(inst, (lambda recs: {}), gen=(lambda: iter(())) if is_origin else None, inputs=inputs, outputs=outputs, events=events, pre=pre))


# ---- JDBC ------------------------------------------------------------------------------------------------------------------------------------
_JDBC = {"mysql": "mysql+pymysql", "mariadb": "mysql+pymysql", "postgresql": "postgresql+psycopg2", "redshift": "redshift+psycopg2"}


def sa_url(inst: str, jdbc_url: str, user: str = "", password: str = "") -> str:
    env = os.environ.get(f"SDC_SQLALCHEMY_URL_{re.sub(r'[^A-Za-z0-9]', '_', inst).upper()}")
    if env:
        return env
    m = re.match(r"^jdbc:([a-z0-9]+)://(.*)$", jdbc_url or "")
    if not m or m.group(1) not in _JDBC:
        raise SdcCaveat(f"SDC-CAVEAT: {inst}: cannot derive a SQLAlchemy URL from {jdbc_url!r}; set SDC_SQLALCHEMY_URL_{re.sub(r'[^A-Za-z0-9]', '_', inst).upper()}")
    auth = f"{user}:{password}@" if user else ""
    return f"{_JDBC[m.group(1)]}://{auth}{m.group(2)}"


def _engine(url: str):
    import sqlalchemy as sa
    return sa.create_engine(url)


def jdbc_target(run: Run, inst: str, url_fn, table_fn, op: str, *, inputs, on_error="TO_ERROR", pre=(), req=(), key_columns=()):
    """JDBC Producer, default operation INSERT (UPDATE/DELETE need key columns and are translated by the converter only when given)."""
    import sqlalchemy as sa

    def go(recs):
        by_table: dict = {}
        for r in recs:
            by_table.setdefault(table_fn(r), []).append(r.value)
        for table, rows in by_table.items():
            if run.dry:
                run.log(f"[dry run] {inst}: would {op} {len(rows)} row(s) into {table}")
                continue
            eng = _engine(url_fn())
            tbl = sa.Table(table, sa.MetaData(), autoload_with=eng)
            with eng.begin() as conn:
                if op == "INSERT":
                    conn.execute(tbl.insert(), [{k: v for k, v in row.items() if k in tbl.c} for row in rows])
                else:
                    raise SdcCaveat(f"SDC-CAVEAT: {inst}: operation {op} is not translated")
        return {}
    run.add(Stage(inst, go, inputs=inputs, on_error=on_error, pre=pre, req=req))


def jdbc_query_executor(run: Run, inst: str, url_fn, queries, *, inputs, on_error="TO_ERROR", pre=(), req=()):
    """JDBC Query executor: runs each query (an EL string evaluated per record) once per record."""
    import sqlalchemy as sa

    def go(recs):
        for r in recs:
            for q in queries:
                sql = q(r)
                if run.dry:
                    run.log(f"[dry run] {inst}: would run: {sql}")
                    continue
                with _engine(url_fn()).begin() as conn:
                    conn.execute(sa.text(sql))
        return {}
    run.add(Stage(inst, go, inputs=inputs, on_error=on_error, pre=pre, req=req))


def jdbc_multitable_origin(run: Run, inst: str, url_fn, patterns: list[str], batch: int, *, outputs, events=()):
    """JDBC Multitable Consumer, non-incremental: every table matching a pattern is read in full. Each record carries `jdbc.tables`."""
    import sqlalchemy as sa

    def gen():
        if run.dry:
            run.log(f"[dry run] {inst}: would read tables matching {patterns}")
            return
        eng = _engine(url_fn())
        insp = sa.inspect(eng)
        names = insp.get_table_names()
        for t in names:
            if not any(re.fullmatch(re.escape(p).replace("%", ".*").replace("_", "."), t) if False else _like(p, t) for p in patterns):
                continue
            with eng.connect() as conn:
                res = conn.execution_options(stream_results=True).execute(sa.text(f"SELECT * FROM {t}"))
                cols = list(res.keys())
                while True:
                    rows = res.fetchmany(batch)
                    if not rows:
                        break
                    yield {outputs[0]: [Record(dict(zip(cols, row)), {"jdbc.tables": t}) for row in rows]}
    run.add(Stage(inst, gen=gen, outputs=outputs, events=events))


def _like(pattern: str, name: str) -> bool:
    return re.fullmatch("".join(".*" if c == "%" else "." if c == "_" else re.escape(c) for c in pattern), name, re.I) is not None


def s3_origin(run: Run, inst: str, bucket_fn, prefix_fn, pattern: str, fmt: str, *, header: bool, delimiter: str, outputs, events, batch: int = 1000,
              key_fn=None, secret_fn=None, region: str = ""):
    """Amazon S3 origin for DELIMITED (CSV) and JSON: lists `<bucket>/<prefix>` matching `pattern`, emits batches, and a finished-file event per object."""
    def gen():
        if run.dry:
            run.log(f"[dry run] {inst}: would read s3://{bucket_fn()}{prefix_fn()} matching {pattern}")
            return
        import fsspec
        kw = {}
        if key_fn and key_fn():
            kw = {"key": key_fn(), "secret": secret_fn()}
        fs = fsspec.filesystem("s3", **kw)
        base = f"{bucket_fn()}/{prefix_fn().strip('/')}"
        mark = run.offsets_in.get(inst, "")
        found = [(str(fs.info(p).get("LastModified", "")), p) for p in fs.find(base) if fnmatch.fnmatch(p.rsplit("/", 1)[-1], pattern)]
        for stamp, path in sorted(x for x in found if x[0] > mark or not mark):
            run.offsets_out[inst] = max(run.offsets_out.get(inst, ""), stamp)
            n = 0
            key = path.lstrip("/").split("/", 1)[-1]          # the object key (prefix included), which is what SDC reports as `Name`
            meta = {"Name": key, "s3.bucket": bucket_fn(), "s3.object.key": key}
            with fs.open(path, "rt", newline="") as fh:
                rows = _parse(fh, fmt, header, delimiter)
                buf = []
                for row in rows:
                    buf.append(Record(row, dict(meta)))
                    n += 1
                    if len(buf) >= batch:
                        yield {outputs[0]: buf}
                        buf = []
                if buf:
                    yield {outputs[0]: buf}
            if events:
                yield {events[0]: [Record({"filepath": key, "filename": key.rsplit("/", 1)[-1], "record-count": n, "error-count": 0}, dict(meta), "finished-file")]}
    run.add(Stage(inst, gen=gen, outputs=outputs, events=events))


_APACHE = (r'(?P<clientip>\S+) (?P<ident>\S+) (?P<auth>\S+) \[(?P<timestamp>[^\]]+)\] "(?:(?P<verb>\w+) (?P<request>\S+)(?: HTTP/(?P<httpversion>[0-9.]+))?'
           r'|(?P<rawrequest>[^"]*))" (?P<response>\d+) (?:(?P<bytes>\d+)|-)')
_LOG_PATTERNS = {
    "COMMONAPACHELOG": re.compile("^" + _APACHE + "$"),
    "COMBINEDAPACHELOG": re.compile("^" + _APACHE + r' (?P<referrer>"[^"]*"|\S+) (?P<agent>"[^"]*"|\S+)$'),
}


def parse_log(line: str, mode: str):
    """mode: COMMONAPACHELOG or COMBINEDAPACHELOG (grok), or COMMON_LOG_FORMAT / COMBINED_LOG_FORMAT, which Data Collector 6.4.1 parses into the same field names
    (clientip, ident, auth, timestamp, verb, request, httpversion, rawrequest, response, bytes; combined adds referrer and agent). A group that did not match is null.
    Unparsable lines raise."""
    mode = {"COMMON_LOG_FORMAT": "COMMONAPACHELOG", "COMBINED_LOG_FORMAT": "COMBINEDAPACHELOG"}.get(mode, mode)
    m = _LOG_PATTERNS[mode].match(line.rstrip("\n"))
    if not m:
        raise ValueError(f"line does not match {mode}: {line[:80]!r}")
    return dict(m.groupdict())


def _parse(fh, fmt: str, header: bool, delimiter: str, log_mode: str = "", quote: str = '"'):
    import csv
    if fmt == "LOG":
        for line in fh:
            if line.strip():
                yield parse_log(line, log_mode)
        return
    if fmt == "DELIMITED":
        rd = csv.reader(fh, delimiter=delimiter, quotechar=quote)
        cols = None
        for i, row in enumerate(rd):
            if not row:
                continue
            if header and cols is None:
                cols = row
                continue
            yield dict(zip(cols, row)) if cols else {str(j): v for j, v in enumerate(row)}
    elif fmt == "JSON":
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)
    else:
        raise SdcCaveat(f"SDC-CAVEAT: data format {fmt} is not translated")


# ---- more processors ------------------------------------------------------------------------------------------------------------------------
def _stage(run, inst, fn, *, inputs, outputs, on_error, pre, req=(), events=()):
    st = Stage(inst, fn, inputs=inputs, outputs=outputs, events=events, on_error=on_error, pre=pre, req=req)
    run.add(st)
    return st


def _map_records(run, inst, one, *, inputs, outputs, on_error="TO_ERROR", pre=(), req=()):
    holder: dict = {}
    each = _each(holder, run, one)
    holder["st"] = _stage(run, inst, lambda recs: {outputs[0]: each(recs)}, inputs=inputs, outputs=outputs, on_error=on_error, pre=pre, req=req)


_CONV = {
    "BOOLEAN": lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("true", "1", "t", "yes"),
    "BYTE": lambda v: int(float(v)), "SHORT": lambda v: int(float(v)), "INTEGER": lambda v: int(float(v)), "LONG": lambda v: int(float(v)),
    "FLOAT": float, "DOUBLE": float, "STRING": lambda v: v if isinstance(v, str) else str(v),
}


def field_type_converter(run: Run, inst: str, rules, *, inputs, outputs, on_error="TO_ERROR", pre=(), req=()):
    """Field Type Converter by field: rules = [([paths], TARGET_TYPE[, java date pattern])]; `/*` means every top-level field. DECIMAL keeps full
    precision; DATE and DATETIME parse with the Java pattern and give a datetime."""
    import decimal

    def conv(t, v, fmt=None):
        if v is None:
            return None
        if t == "DECIMAL":
            return decimal.Decimal(str(v).strip())
        if t in ("DATE", "DATETIME"):
            return parse_date(v, fmt)
        return _CONV[t](v)

    def one(r: Record):
        for rule in rules:
            paths, t, fmt = rule[0], rule[1], (rule[2] if len(rule) > 2 else None)
            for p in paths:
                for q in (list(r.value) and [f"/{k}" for k in r.value]) if p == "/*" else [p]:
                    cur = get_path(r.value, q, _MISSING)
                    if cur is _MISSING:
                        continue
                    set_path(r.value, q, conv(t, cur, fmt))
        return r
    _map_records(run, inst, one, inputs=inputs, outputs=outputs, on_error=on_error, pre=pre, req=req)


def field_renamer(run: Run, inst: str, mapping, missing: str, existing: str, *, inputs, outputs, on_error="TO_ERROR", pre=(), req=()):
    """Field Renamer for literal paths. missing: TO_ERROR | CONTINUE (source absent). existing: TO_ERROR | REPLACE (target present)."""
    def one(r: Record):
        for src, dst in mapping:
            v = get_path(r.value, src, _MISSING)
            if v is _MISSING:
                if missing == "TO_ERROR":
                    raise KeyError(f"field {src} does not exist")
                continue
            if get_path(r.value, dst, _MISSING) is not _MISSING and existing == "TO_ERROR":
                raise KeyError(f"field {dst} already exists")
            del_path(r.value, src)
            set_path(r.value, dst, v)
        return r
    _map_records(run, inst, one, inputs=inputs, outputs=outputs, on_error=on_error, pre=pre, req=req)


def field_order(run: Run, inst: str, fields: list[str], missing: str, extra: str, *, inputs, outputs, on_error="TO_ERROR", pre=(), req=()):
    def one(r: Record):
        out = {}
        for f in fields:
            v = get_path(r.value, f, _MISSING)
            if v is _MISSING:
                if missing == "TO_ERROR":
                    raise KeyError(f"field {f} is missing")
                v = None
            set_path(out, f, v)
        if extra == "TO_ERROR" and set(r.value) - set(out):
            raise KeyError(f"extra fields {sorted(set(r.value) - set(out))}")
        r.value = out
        return r
    _map_records(run, inst, one, inputs=inputs, outputs=outputs, on_error=on_error, pre=pre, req=req)


def splitter(run: Run, inst: str, path: str, sep: str, targets: list[str], too_many: str, keep_original: bool, *, inputs, outputs, on_error="TO_ERROR", pre=(), req=()):
    """Field Splitter: separator is a regular expression; too_many TO_LAST_FIELD keeps the remainder in the last field, TO_LIST makes a `list` field."""
    def one(r: Record):
        v = get_path(r.value, path, _MISSING)
        if v is _MISSING or v is None:
            raise KeyError(f"field {path} is missing")
        parts = re.split(sep, str(v), maxsplit=len(targets) - 1 if too_many == "TO_LAST_FIELD" else 0)
        if len(parts) < len(targets):
            raise ValueError(f"field {path} has {len(parts)} part(s), fewer than the {len(targets)} split fields")      # as Data Collector 3.18.1 does
        for i, t in enumerate(targets):
            set_path(r.value, t, parts[i])
        if not keep_original:
            del_path(r.value, path)
        return r
    _map_records(run, inst, one, inputs=inputs, outputs=outputs, on_error=on_error, pre=pre, req=req)


def json_parser(run: Run, inst: str, src: str, dst: str, *, inputs, outputs, on_error="TO_ERROR", pre=(), req=()):
    """Data Parser for a JSON string field (first value only)."""
    def one(r: Record):
        v = get_path(r.value, src, _MISSING)
        if v is _MISSING:
            raise KeyError(f"field {src} is missing")
        set_path(r.value, dst, json.loads(v) if isinstance(v, (str, bytes)) else v)
        return r
    _map_records(run, inst, one, inputs=inputs, outputs=outputs, on_error=on_error, pre=pre, req=req)


def field_merger(run: Run, inst: str, mappings, overwrite: bool, *, inputs, outputs, on_error="TO_ERROR", pre=(), req=()):
    """Field Merger (checked against Data Collector 6.4.1): both fields must be maps or both lists, else a record error. The source field is moved. A missing target takes the
    source; lists append the source's items after the target's; maps merge, and a clashing key is a record error unless overwrite is on."""
    def one(r: Record):
        for src, dst in mappings:
            s = get_path(r.value, src, _MISSING)
            if s is _MISSING:
                raise KeyError(f"field {src} does not exist")
            if not isinstance(s, (dict, list)):
                raise TypeError("Field merger only supports LIST and MAP types")
            d = get_path(r.value, dst, _MISSING)
            del_path(r.value, src)
            if d is _MISSING:
                set_path(r.value, dst, s)
            elif type(d) is not type(s):
                raise TypeError(f"Cannot merge field of type {type(s).__name__} with field of type {type(d).__name__}")
            elif isinstance(s, list):
                d.extend(s)
            else:
                if not overwrite and any(k in d for k in s):
                    raise KeyError(f"Fields '{dst}' cannot be overwritten")
                d.update(s)
        return r
    _map_records(run, inst, one, inputs=inputs, outputs=outputs, on_error=on_error, pre=pre, req=req)


def _xml_map(el):
    """An element as Data Collector's XML Parser shapes it (checked against 6.4.1): attributes as `attr|name`, text as `value`, children grouped by tag into lists of such maps."""
    out: dict = {}
    for k, v in el.attrib.items():
        out["attr|" + k.rsplit("}", 1)[-1]] = v
    kids = list(el)
    if (el.text or "").strip() and not kids:
        out["value"] = el.text.strip()
    for k in kids:
        out.setdefault(k.tag.rsplit("}", 1)[-1], []).append(_xml_map(k))
    return out


def xml_parser(run: Run, inst: str, src: str, dst: str, element: str | None, multiple: str, *, inputs, outputs, on_error="TO_ERROR", pre=(), req=()):
    """XML Parser: parses the XML string at `src` into `dst`. Without a delimiter element the document's content becomes one map; with one, FIRST_ONLY keeps the first match,
    ALL_AS_LIST a list, SPLIT_INTO_MULTIPLE_RECORDS one record per match. Attributes are fields (`attr|name`), not field attributes."""
    import xml.etree.ElementTree as ET
    holder: dict = {}

    def one(r: Record):
        text = get_path(r.value, src, _MISSING)
        if text is _MISSING or text is None:
            raise KeyError(f"field {src} is missing")
        root = ET.fromstring(text if isinstance(text, (str, bytes)) else str(text))
        if not element:
            set_path(r.value, dst, _xml_map(root))
            return r
        found = [_xml_map(e) for e in root.findall(element)]
        if not found:
            raise ValueError(f"no <{element}> element in the document")
        if multiple == "ALL_AS_LIST":
            set_path(r.value, dst, found)
            return r
        if multiple == "FIRST_ONLY":
            set_path(r.value, dst, found[0])
            return r
        outs = []
        for f in found:
            n = r.copy()
            set_path(n.value, dst, f)
            outs.append(n)
        return outs
    each = _each(holder, run, one)
    holder["st"] = _stage(run, inst, lambda recs: {outputs[0]: each(recs)}, inputs=inputs, outputs=outputs, on_error=on_error, pre=pre, req=req)


def field_replacer(run: Run, inst: str, rules, missing: str, *, inputs, outputs, on_error="TO_ERROR", pre=(), req=()):
    """Field Replacer (6.x, checked against Data Collector 6.4.1): rules = [([paths], set_to_null, replacement_fn)]. The replacement is an expression evaluated per record (a plain
    string is a literal) and its typed result replaces the field. A field that does not exist: ADD_FIELD creates it, CONTINUE skips it, TO_ERROR is a record error."""
    def one(r: Record):
        for paths, null, fn in rules:
            for p in paths:
                if get_path(r.value, p, _MISSING) is _MISSING:
                    if missing == "TO_ERROR":
                        raise KeyError(f"Rule {p} did not match any fields")
                    if missing == "CONTINUE" or null:
                        continue
                set_path(r.value, p, None if null else fn(r))
        return r
    _map_records(run, inst, one, inputs=inputs, outputs=outputs, on_error=on_error, pre=pre, req=req)


# ---- hand-written replacements for stages (scripts, retired Hadoop, custom origins) -----------------------------------------------------------
class _Lanes:
    def __init__(self, lanes):
        self.lanes, self.out = list(lanes), {}

    def write(self, record, lane=None, *_):
        if lane is None:
            lane = self.lanes[0] if self.lanes else None
        elif isinstance(lane, int):
            lane = self.lanes[lane]
        if lane is None:
            raise RuntimeError("this stage has no output lane")
        self.out.setdefault(lane, []).append(record)


class Sdc:
    """What a script sees: `sdc.records`, `sdc.output.write(record, lane)`, `sdc.error.write(record, msg)`, `sdc.state`, `sdc.log`."""

    def __init__(self, run: Run, lanes, state: dict):
        import logging
        self.records, self.output, self.state, self.log = [], _Lanes(lanes), state, logging.getLogger("sdc")
        self.errors: list = []
        self.error = type("E", (), {"write": lambda _s, rec, msg="": self.errors.append((rec, msg))})()

    @staticmethod
    def create_record(value=None, attributes=None, event=None) -> Record:
        return Record(value or {}, dict(attributes or {}), event)


def override(run: Run, inst: str, fn, *, inputs, outputs=(), events=(), on_error="TO_ERROR", pre=(), req=()):
    """A person-written replacement for a processor, destination or executor: `fn(records, sdc)`; write records with `sdc.output.write`."""
    state: dict = {}
    holder: dict = {}

    def go(recs):
        sdc = Sdc(run, [*outputs, *events], state)
        sdc.records = recs
        fn(recs, sdc)
        for rec, msg in sdc.errors:
            run._err(holder["st"], rec, msg or "script error")
        return sdc.output.out
    holder["st"] = _stage(run, inst, go, inputs=inputs, outputs=outputs, events=events, on_error=on_error, pre=pre, req=req)


def override_origin(run: Run, inst: str, produce, *, outputs, events=()):
    """A person-written origin: `produce(sdc)` yields lists of records (or {lane: records})."""
    state: dict = {}

    def gen():
        sdc = Sdc(run, [*outputs, *events], state)
        sdc.offsets = run.offsets_in.get(inst)
        for batch in produce(sdc):
            yield batch if isinstance(batch, dict) else {outputs[0]: list(batch)}
        run.offsets_out[inst] = state.get("offset", run.offsets_out.get(inst))
    run.add(Stage(inst, gen=gen, outputs=outputs, events=events))


def _py2_division(source: str, name: str):
    """Jython is Python 2.7, where `/` on two integers is integer (floor) division. Compile the script with `/` and `/=` replaced by a helper that behaves that way
    (true division when either operand is a float), the one Python 2 behaviour that silently changes numbers. Other Python 2 differences still fail loudly."""
    import ast

    class T(ast.NodeTransformer):
        def visit_BinOp(self, node):
            self.generic_visit(node)
            if isinstance(node.op, ast.Div):
                return ast.copy_location(ast.Call(func=ast.Name(id="__py2div__", ctx=ast.Load()), args=[node.left, node.right], keywords=[]), node)
            return node

        def visit_AugAssign(self, node):
            self.generic_visit(node)
            if isinstance(node.op, ast.Div):
                load = ast.copy_location(ast.Name(id=getattr(node.target, "id", "_"), ctx=ast.Load()), node) if isinstance(node.target, ast.Name) else None
                if load is not None:
                    call = ast.Call(func=ast.Name(id="__py2div__", ctx=ast.Load()), args=[load, node.value], keywords=[])
                    return ast.copy_location(ast.Assign(targets=[node.target], value=call), node)
            return node
    tree = T().visit(ast.parse(source, name))
    ast.fix_missing_locations(tree)
    return tree


def __py2div__(a, b):
    return a // b if isinstance(a, int) and isinstance(b, int) and not isinstance(a, bool) else a / b


def jython_processor(run: Run, inst: str, script: str, init: str, destroy: str, mode: str, *, user_params: dict | None = None, inputs, outputs, events=(), on_error="TO_ERROR", pre=(), req=()):
    """Jython Evaluator, run unchanged under CPython with the documented scripting objects: sdc.records, sdc.output.write(record[, lane]),
    sdc.error.write(record, msg), sdc.state, sdc.log, and the globals `records`, `output`, `error`, `log`, `state` (and `record` in RECORD mode).
    Python 2 syntax and Java imports fail with the interpreter's own error."""
    state: dict = {}
    holder: dict = {}
    code = compile(_py2_division(script, f"<{inst} script>"), f"<{inst} script>", "exec")

    def namespace(sdc):
        sdc.userParams = dict(user_params or {})
        return {"sdc": sdc, "records": sdc.records, "output": sdc.output, "error": sdc.error, "log": sdc.log, "state": state, "__py2div__": __py2div__}
    if init.strip():
        exec(compile(init, f"<{inst} init>", "exec"), namespace(Sdc(run, [*outputs, *events], state)))
    run.closers.append(lambda: destroy.strip() and exec(compile(destroy, f"<{inst} destroy>", "exec"), namespace(Sdc(run, [*outputs, *events], state))))

    def go(recs):
        sdc = Sdc(run, [*outputs, *events], state)
        sdc.records = recs
        if mode == "RECORD":
            for r in recs:
                ns = namespace(sdc)
                ns["record"] = r
                exec(code, ns)
        else:
            exec(code, namespace(sdc))
        for rec, msg in sdc.errors:
            run._err(holder["st"], rec, msg or "script error")
        return sdc.output.out
    holder["st"] = _stage(run, inst, go, inputs=inputs, outputs=outputs, events=events, on_error=on_error, pre=pre, req=req)


def local_lookup(run: Run, inst: str, table: dict, lookups, *, inputs, outputs, on_error="TO_ERROR", pre=(), req=()):
    """Local Lookup: lookups = [(key_fn, output_path)]; the static table maps keys to values. A key with no entry leaves the output field unset."""
    def one(r: Record):
        for key_fn, path in lookups:
            k = str(key_fn(r))
            if k in table:                               # a key with no entry leaves the field unset (checked against Data Collector 6.4.1)
                set_path(r.value, path, table[k])
        return r
    _map_records(run, inst, one, inputs=inputs, outputs=outputs, on_error=on_error, pre=pre, req=req)


# ---- HTTP -------------------------------------------------------------------------------------------------------------------------------------
def _http(run: Run, inst: str, method: str, url: str, *, body=None, headers=None, auth=None):
    import requests
    resp = requests.request(method, url, data=body, headers=headers or {}, auth=auth, timeout=60)
    if resp.status_code >= 400:
        raise RuntimeError(f"{method} {url.split('?')[0]} returned HTTP {resp.status_code}: {resp.text[:200]}")
    return resp


def http_processor(run: Run, inst: str, url_fn, method: str, body_fn, headers_fn, auth_fn, out_field: str, *, inputs, outputs, on_error="TO_ERROR", pre=(), req=()):
    """HTTP Client processor (JSON response, no pagination): the response body is parsed and written to `out_field`."""
    def one(r: Record):
        if run.dry:
            run.log(f"[dry run] {inst}: would {method} {str(url_fn(r)).split('?')[0]}")
            set_path(r.value, out_field, None)
            return r
        resp = _http(run, inst, method, url_fn(r), body=body_fn(r) if body_fn else None, headers=headers_fn(r), auth=auth_fn())
        set_path(r.value, out_field, resp.json() if resp.content else None)
        return r
    _map_records(run, inst, one, inputs=inputs, outputs=outputs, on_error=on_error, pre=pre, req=req)


def http_target(run: Run, inst: str, url_fn, method: str, headers_fn, auth_fn, json_mode: str, per_batch: bool, *, inputs, on_error="TO_ERROR", pre=(), req=()):
    """HTTP Client destination, JSON body: one request per batch (ARRAY_OBJECTS: a JSON array; otherwise one object per line) or one per record."""
    holder: dict = {}

    def send(recs, body):
        if run.dry:
            run.log(f"[dry run] {inst}: would {method} {str(url_fn(recs[0])).split('?')[0]} ({len(recs)} record(s))")
            return
        _http(run, inst, method, url_fn(recs[0]), body=body, headers={"Content-Type": "application/json", **headers_fn(recs[0])}, auth=auth_fn())

    def go(recs):
        try:
            if per_batch:
                vals = [r.value for r in recs]
                send(recs, json.dumps(vals if json_mode == "ARRAY_OBJECTS" else None, default=str) if json_mode == "ARRAY_OBJECTS"
                     else "\n".join(json.dumps(v, default=str) for v in vals))
            else:
                for r in recs:
                    send([r], json.dumps(r.value, default=str))
        except Exception as e:                                # noqa: BLE001 - a failed send sends its records to the error policy
            for r in recs:
                run._err(holder["st"], r, f"{type(e).__name__}: {e}")
        return {}
    holder["st"] = _stage(run, inst, go, inputs=inputs, outputs=(), on_error=on_error, pre=pre, req=req)


# ---- JavaScript evaluator (QuickJS) -----------------------------------------------------------------------------------------------------------
_JS_PRELUDE = """
(function () {
  function def(o, n, f) { if (!(n in o)) Object.defineProperty(o, n, {value: f, enumerable: false, writable: true, configurable: true}); }
  def(Array.prototype, 'add', function (a, b) { if (arguments.length === 2) { this.splice(a, 0, b); return; } this.push(a); return true; });
  def(Array.prototype, 'size', function () { return this.length; });
  def(Array.prototype, 'get', function (i) { return this[i]; });
  def(Array.prototype, 'isEmpty', function () { return this.length === 0; });
  def(Array.prototype, 'contains', function (x) { return this.indexOf(x) >= 0; });
  def(Object.prototype, 'put', function (k, v) { var o = this[k]; this[k] = v; return o === undefined ? null : o; });
  def(Object.prototype, 'containsKey', function (k) { return Object.prototype.hasOwnProperty.call(this, k); });
  def(Object.prototype, 'keySet', function () { return Object.keys(this); });
})();
var __out = [], __err = [];
var output = {write: function (r) { __out.push(r); }};
var error = {write: function (r, m) { __err.push([r, String(m)]); }};
var log = {info: function () {}, warn: function () {}, error: function () {}, debug: function () {}, trace: function () {}};
var __ev = [];
var sdc = {log: log, createEvent: function (t, v) { return {value: null, attributes: {}, sourceId: 'event', eventType: t}; }, toEvent: function (e) { __ev.push(e); }};
var sdcFunctions = {createRecord: function (id) { return {value: null, attributes: {}, sourceId: id}; }, createMap: function () { return {}; }};
"""


def js_processor(run: Run, inst: str, script: str, init: str, *, user_params: dict | None = None, inputs, outputs, events=(), on_error="TO_ERROR", pre=(), req=()):
    """JavaScript Evaluator, BATCH mode, run in QuickJS with `records`, `output.write`, `error.write`, `state`, `log` and `sdcFunctions.createRecord`.
    Records cross as JSON (dates and decimals become strings). Nashorn/Java objects (`Java.type`, `new java.*`) do not exist and fail at run time."""
    state: dict = {}
    holder: dict = {}
    counter = [0]

    def run_js(src: str, recs: list) -> dict:
        import quickjs
        payload = [{"value": r.value, "attributes": r.attrs, "sourceId": f"{inst}:{counter[0] + i}"} for i, r in enumerate(recs)]
        counter[0] += len(recs)
        ctx = quickjs.Context()
        ctx.eval("var state = JSON.parse(%s); var records = JSON.parse(%s);" % (json.dumps(json.dumps(state, default=str)), json.dumps(json.dumps(payload, default=str))) + _JS_PRELUDE
                 + "sdc.userParams = JSON.parse(%s);" % json.dumps(json.dumps(user_params or {})))
        ctx.eval(src)
        return json.loads(ctx.eval("JSON.stringify({out: __out, err: __err, ev: __ev, state: state})"))
    started = [False]

    def go(recs):
        if not started[0]:
            started[0] = True
            if init.strip():
                state.update(run_js(init, [])["state"])
        res = run_js(script, recs)
        state.clear()
        state.update(res["state"])
        out = []
        for item in res["out"]:
            if not isinstance(item.get("value"), dict):
                run._err(holder["st"], Record({}), "script wrote a record whose value is not a map")
                continue
            out.append(Record(item["value"], {k: ("" if v is None else str(v)) for k, v in (item.get("attributes") or {}).items()}))
        for item, msg in res["err"]:
            run._err(holder["st"], Record(item.get("value") or {}, {}), msg)
        lanes = {outputs[0]: out} if outputs else {}
        if events and res.get("ev"):
            lanes[events[0]] = [Record(e.get("value") or {}, {}, e.get("eventType")) for e in res["ev"] if isinstance(e.get("value") or {}, dict)]
        return lanes
    holder["st"] = _stage(run, inst, go, inputs=inputs, outputs=outputs, events=events, on_error=on_error, pre=pre, req=req)


def script_native(run: Run, inst: str, module, *, user_params: dict | None = None, inputs, outputs, events=(), on_error="TO_ERROR", pre=(), req=()):
    """A JavaScript Evaluator script that was transpiled to Python (`module.init`, `module.run`, with JavaScript semantics from `jsrt`). Same scripting objects and
    the same JSON boundary as `js_processor` (values are normalised through JSON on the way in and out), so the two behave alike; no JavaScript engine is needed."""
    from . import jsrt
    state: dict = {}
    holder: dict = {}
    counter = [0]
    started = [False]

    class _Log:
        def __getattr__(self, name):
            return lambda *a, **k: UNDEF_RETURN

    UNDEF_RETURN = jsrt.UNDEF

    class _Out:
        def __init__(self):
            self.items: list = []

        def write(self, rec, *_a):
            self.items.append(rec)
            return jsrt.UNDEF

    class _Err:
        def __init__(self):
            self.items: list = []

        def write(self, rec, msg=""):
            self.items.append((rec, jsrt.tostr(msg)))
            return jsrt.UNDEF

    class _Sdc:
        def __init__(self, log):
            self.log, self.events, self.userParams = log, [], dict(user_params or {})

        def createEvent(self, kind, version=1):
            return jsrt.JSRecord(None, {}, "event", jsrt.tostr(kind))

        def toEvent(self, e):
            self.events.append(e)
            return jsrt.UNDEF

    class _Fns:
        createRecord = staticmethod(lambda rid="": jsrt.JSRecord(None, {}, jsrt.tostr(rid)))
        createMap = staticmethod(lambda *_a: {})

    def jin(v):
        return json.loads(json.dumps(v, default=str))

    def go(recs):
        log = _Log()
        sdc = _Sdc(log)
        if not started[0]:
            started[0] = True
            module.init(state, log, sdc, _Fns)
        jr = [jsrt.JSRecord(jin(r.value), {k: v for k, v in jin(r.attrs).items()}, f"{inst}:{counter[0] + i}") for i, r in enumerate(recs)]
        counter[0] += len(recs)
        out, err = _Out(), _Err()
        module.run(jr, out, err, state, log, sdc, _Fns)
        norm = jsrt.normalize(dict(state))
        state.clear()
        state.update(norm)
        good = []
        for rec in out.items:
            v = jsrt.normalize(rec.value)
            if not isinstance(v, dict):
                run._err(holder["st"], Record({}), "script wrote a record whose value is not a map")
                continue
            good.append(Record(v, {k: ("" if x is None else str(x)) for k, x in (jsrt.normalize(rec.attributes) or {}).items()}))
        for rec, msg in err.items:
            run._err(holder["st"], Record(jsrt.normalize(rec.value) if isinstance(rec.value, dict) else {}, {}), msg)
        lanes = {outputs[0]: good} if outputs else {}
        if events and sdc.events:
            lanes[events[0]] = [Record(jsrt.normalize(e.value) if isinstance(e.value, dict) else {}, {}, e.eventType) for e in sdc.events]
        return lanes
    holder["st"] = _stage(run, inst, go, inputs=inputs, outputs=outputs, events=events, on_error=on_error, pre=pre, req=req)


# ---- Jython origin ---------------------------------------------------------------------------------------------------------------------------
class _Offsets(dict):
    containsKey = dict.__contains__
    put = dict.__setitem__


class _Batch:
    def __init__(self, sdc):
        self.sdc, self.recs = sdc, []

    def add(self, rec):
        self.recs.append(rec)

    def process(self, entity, offset):
        self.sdc.batches.append(self.recs)
        self.sdc.offsets[entity] = offset
        self.sdc.stopped = True          # one run is one poll iteration: the script's `while not sdc.isStopped()` loop ends after the first batch
        self.recs = []


class _OriginSdc:
    def __init__(self, params: dict, offsets: dict):
        import logging
        self.userParams, self.lastOffsets, self.offsets = params, _Offsets(offsets), dict(offsets)
        self.batches: list = []
        self.stopped = False
        self.log = logging.getLogger("sdc")

    createRecord = staticmethod(lambda rid: Record({}, {}))
    createBatch = lambda self: _Batch(self)          # noqa: E731
    isStopped = lambda self: self.stopped            # noqa: E731
    importLock = importUnlock = staticmethod(lambda: None)


def jython_origin(run: Run, inst: str, script: str, params: dict, *, outputs, events=()):
    """Jython Scripting origin run under CPython with `sdc.userParams`, `sdc.lastOffsets`, `sdc.createBatch/createRecord`, `batch.add`, `batch.process`
    and `sdc.isStopped`. StreamSets runs the script until the pipeline stops; here it stops after its first processed batch (one poll per run)."""
    def gen():
        if run.dry:
            run.log(f"[dry run] {inst}: would run the origin script once")
            return
        sdc = _OriginSdc(params, run.offsets_in.get(inst) or {})
        exec(compile(script, f"<{inst} script>", "exec"), {"sdc": sdc})
        run.offsets_out[inst] = sdc.offsets
        for b in sdc.batches:
            yield {outputs[0]: b}
    run.add(Stage(inst, gen=gen, outputs=outputs, events=events))


def file_tail_origin(run: Run, inst: str, paths: list[str], fmt: str, log_mode: str, batch: int, *, outputs, events=()):
    """File Tail as one batch: reads each file from the stored byte offset to the end (a file shorter than its offset restarts at 0).
    Records carry `offset`, `filename`, `file` and `mtime`. Rolled files are not followed."""
    def gen():
        if run.dry:
            run.log(f"[dry run] {inst}: would read new lines of {paths}")
            return
        offs = dict(run.offsets_in.get(inst) or {})
        for p in paths:
            if not os.path.exists(p):
                continue
            start = offs.get(p, 0)
            if os.path.getsize(p) < start:
                start = 0
            buf = []
            with open(p, "rb") as fh:
                fh.seek(start)
                pos = start
                for raw in iter(fh.readline, b""):
                    here, pos = pos, pos + len(raw)
                    if not raw.endswith(b"\n"):                 # a partial last line is read on the next run
                        pos = here
                        break
                    line = raw.decode("utf-8", "replace")
                    attrs = {"offset": str(here), "filename": os.path.basename(p), "file": p, "mtime": str(int(os.path.getmtime(p) * 1000))}
                    try:
                        value = next(iter(_parse([line], fmt, False, ",", log_mode)))
                    except ValueError:
                        value = None
                    if value is None:
                        run.counts["errors"][inst] = run.counts["errors"].get(inst, 0) + 1
                        continue
                    buf.append(Record(value, attrs))
                    if len(buf) >= batch:
                        yield {outputs[0]: buf}
                        buf = []
            if buf:
                yield {outputs[0]: buf}
            offs[p] = pos
        run.offsets_out[inst] = offs
    run.add(Stage(inst, gen=gen, outputs=outputs, events=events))


def raw_data_origin(run: Run, inst: str, text: str, fmt: str, *, header: bool = True, delimiter: str = ",", outputs, events=()):
    """Dev Raw Data Source: emits the configured text once (JSON objects, or delimited rows)."""
    def gen():
        import io
        if fmt == "JSON":
            dec, i, out = json.JSONDecoder(), 0, []
            while i < len(text):
                if text[i].isspace():
                    i += 1
                    continue
                obj, i = dec.raw_decode(text, i)
                out.append(Record(obj if isinstance(obj, dict) else {"value": obj}))
        else:
            out = [Record(row) for row in _parse(io.StringIO(text), fmt, header, delimiter)]
        yield {outputs[0]: out}
    run.add(Stage(inst, gen=gen, outputs=outputs, events=events))


def geolocation(run: Run, inst: str, db_file: str, conversions, missing: str, *, inputs, outputs, on_error="TO_ERROR", pre=(), req=()):
    """Geo-IP lookup in a MaxMind GeoIP2 City database (`.mmdb`). conversions = [(TARGET, input path, output path)]; TARGET is one of LATITUDE,
    LONGITUDE, COUNTRY_NAME, COUNTRY_ISO_CODE, CITY_NAME, POSTAL_CODE. An address with no entry is a record error. Set SDC_GEOIP_DB to override the path."""
    reader: list = []
    get = {"LATITUDE": lambda d: d["location"]["latitude"], "LONGITUDE": lambda d: d["location"]["longitude"],
           "COUNTRY_NAME": lambda d: d["country"]["names"]["en"], "COUNTRY_ISO_CODE": lambda d: d["country"]["iso_code"],
           "CITY_NAME": lambda d: d["city"]["names"]["en"], "POSTAL_CODE": lambda d: d["postal"]["code"]}

    def one(r: Record):
        if not reader:
            import maxminddb
            reader.append(maxminddb.open_database(os.environ.get("SDC_GEOIP_DB", db_file)))
        cache = {}
        for target, src, dst in conversions:
            addr = get_path(r.value, src)
            if addr not in cache:
                cache[addr] = reader[0].get(str(addr))
            rec = cache[addr]
            if rec is None:
                raise KeyError(f"no geolocation entry for {addr}")
            try:
                set_path(r.value, dst, get[target](rec))
            except KeyError:
                raise KeyError(f"{target} is not in the database entry for {addr}")
        return r
    run.closers.append(lambda: reader and reader[0].close())
    _map_records(run, inst, one, inputs=inputs, outputs=outputs, on_error=on_error, pre=pre, req=req)


def field_mask(run: Run, inst: str, rules, *, inputs, outputs, on_error="TO_ERROR", pre=(), req=()):
    """Field Masker: rules = [([paths], MASK_TYPE, regex, groups_to_show[, custom mask])]. Mask character is `x`. FIXED_LENGTH gives ten x's; VARIABLE_LENGTH one x
    per character; REGEX keeps the listed groups and masks the rest (a value that does not match is left as it is); CUSTOM: `#` shows the character, any other
    character is the mask."""
    def mask(v, kind, regex, show, custom=""):
        v = str(v)
        if kind == "CUSTOM":
            return "".join(v[i] if custom[i] == "#" else custom[i] for i in range(min(len(v), len(custom))))
        if kind == "VARIABLE_LENGTH":
            return "x" * len(v)
        if kind == "FIXED_LENGTH":
            return "x" * 10
        m = re.fullmatch(regex, v)
        if not m:
            return v                                       # a value that does not match is left as it is (Data Collector 3.18.1)
        out = ""
        for i in range(1, (m.re.groups or 0) + 1):
            g = m.group(i) or ""
            out += g if i in show else "x" * len(g)
        return out

    def one(r: Record):
        for rule in rules:
            paths, kind, regex, show = rule[:4]
            custom = rule[4] if len(rule) > 4 else ""
            for p in paths:
                v = get_path(r.value, p, _MISSING)
                if v is not _MISSING and v is not None:
                    set_path(r.value, p, mask(v, kind, regex, show, custom))
        return r
    _map_records(run, inst, one, inputs=inputs, outputs=outputs, on_error=on_error, pre=pre, req=req)


# ---- Kafka (confluent-kafka) ------------------------------------------------------------------------------------------------------------------
def kafka_origin(run: Run, inst: str, brokers: str, group: str, topics: list[str], reset: str, batch: int, wait_ms: int, *, outputs, events=()):
    """Kafka consumer as a micro-batch: reads from the group's committed offsets until a poll returns nothing, then stops. Offsets are committed only
    after the whole run succeeds. Values must be JSON. Records carry `topic`, `partition`, `offset`."""
    def gen():
        if run.dry:
            run.log(f"[dry run] {inst}: would consume {topics} as group {group}")
            return
        import confluent_kafka as ck
        c = ck.Consumer({"bootstrap.servers": brokers, "group.id": group, "auto.offset.reset": reset.lower(), "enable.auto.commit": False})
        c.subscribe(topics)
        run.on_success.append(lambda: c.commit(asynchronous=False))
        run.closers.append(c.close)
        while True:
            msgs = c.consume(num_messages=batch, timeout=wait_ms / 1000)
            good = [m for m in msgs if m.error() is None]
            if not good:
                break
            recs = []
            for m in good:
                try:
                    v = json.loads(m.value())
                except ValueError:
                    run.counts["errors"][inst] = run.counts["errors"].get(inst, 0) + 1
                    continue
                recs.append(Record(v if isinstance(v, dict) else {"value": v},
                                   {"topic": m.topic(), "partition": str(m.partition()), "offset": str(m.offset())}))
            if recs:
                yield {outputs[0]: recs}
    run.add(Stage(inst, gen=gen, outputs=outputs, events=events))


def kafka_target(run: Run, inst: str, brokers: str, topic_fn, *, inputs, on_error="TO_ERROR", pre=(), req=()):
    """Kafka producer: one JSON message per record to `topic_fn(record)`; flushed at the end of each batch."""
    prod: list = []

    def go(recs):
        if run.dry:
            run.log(f"[dry run] {inst}: would produce {len(recs)} message(s)")
            return {}
        if not prod:
            import confluent_kafka as ck
            prod.append(ck.Producer({"bootstrap.servers": brokers}))
        for r in recs:
            prod[0].produce(topic_fn(r), json.dumps(r.value, default=str).encode())
        prod[0].flush()
        return {}
    run.add(Stage(inst, go, inputs=inputs, on_error=on_error, pre=pre, req=req))


# ---- encryption, decryption and hashing ---------------------------------------------------------------------------------------------------------
def _crypto_client():
    """AWS Encryption SDK client. Data Collector writes (and expects) the older non-committing algorithm suites, so reading and writing StreamSets-compatible ciphertext needs
    FORBID_ENCRYPT_ALLOW_DECRYPT; the SDK's default policy refuses to decrypt them. This weakens the SDK's default key-commitment guarantee on purpose, for compatibility only."""
    import aws_encryption_sdk
    from aws_encryption_sdk.identifiers import CommitmentPolicy
    return aws_encryption_sdk.EncryptionSDKClient(commitment_policy=CommitmentPolicy.FORBID_ENCRYPT_ALLOW_DECRYPT)


def _crypto_provider(kms_key_arn: str | None, raw_key_b64: str | None, key_id: str):
    """Master key provider: AWS KMS (needs credentials and network), or a user-supplied Base64 AES key (16, 24 or 32 bytes) under the provider id Data Collector uses for its
    'User Supplied Key' option, `JceMasterKeyProvider`, with the stage's Key ID (verified against Data Collector 6.4.1)."""
    if kms_key_arn:
        from aws_encryption_sdk import StrictAwsKmsMasterKeyProvider
        return StrictAwsKmsMasterKeyProvider(key_ids=[kms_key_arn])
    if not raw_key_b64:
        raise SdcCaveat("SDC-CAVEAT: encryption needs a KMS key ARN or a Base64 key (SDC_SECRET_* or an environment variable)")
    import base64
    from aws_encryption_sdk.identifiers import EncryptionKeyType, WrappingAlgorithm
    from aws_encryption_sdk.internal.crypto.wrapping_keys import WrappingKey
    from aws_encryption_sdk.key_providers.raw import RawMasterKeyProvider
    key = base64.b64decode(raw_key_b64)
    alg = {16: WrappingAlgorithm.AES_128_GCM_IV12_TAG16_NO_PADDING, 24: WrappingAlgorithm.AES_192_GCM_IV12_TAG16_NO_PADDING,
           32: WrappingAlgorithm.AES_256_GCM_IV12_TAG16_NO_PADDING}.get(len(key))
    if alg is None:
        raise SdcCaveat(f"SDC-CAVEAT: a user-supplied key must be 16, 24 or 32 bytes, got {len(key)}")

    class _Provider(RawMasterKeyProvider):
        provider_id = "JceMasterKeyProvider"

        def _get_raw_key(self, key_id_):
            return WrappingKey(wrapping_algorithm=alg, wrapping_key=key, wrapping_key_type=EncryptionKeyType.SYMMETRIC)
    p = _Provider()
    p.add_master_key(key_id or "")
    return p


def encrypt_value(value, *, kms_key_arn: str | None = None, raw_key_b64: str | None = None, key_id: str = "", context: dict | None = None) -> bytes:
    """Encrypt one field value the way Data Collector's Encrypt and Decrypt Fields stage does (verified against 6.4.1, both directions): an AWS Encryption SDK message
    whose plaintext is the value's text in UTF-8 (`42`, `2.5`, `true`) and whose encryption context carries `SDC_FIELD_TYPE` (STRING, INTEGER, LONG, DOUBLE, BOOLEAN, DECIMAL,
    BYTE_ARRAY) plus your own entries. Data Collector can decrypt this, and this can decrypt Data Collector's. Date and time types are not supported (not yet verified)."""
    import decimal
    if isinstance(value, bool):
        kind, plain = "BOOLEAN", b"true" if value else b"false"
    elif isinstance(value, int):
        kind, plain = ("INTEGER" if -2**31 <= value < 2**31 else "LONG"), str(value).encode()
    elif isinstance(value, float):
        kind, plain = "DOUBLE", repr(value).encode()
    elif isinstance(value, decimal.Decimal):
        kind, plain = "DECIMAL", str(value).encode()
    elif isinstance(value, str):
        kind, plain = "STRING", value.encode()
    elif isinstance(value, (bytes, bytearray)):
        kind, plain = "BYTE_ARRAY", bytes(value)
    else:
        raise TypeError(f"cannot encrypt a value of type {type(value).__name__} (dates and times are not supported yet)")
    blob, _ = _crypto_client().encrypt(source=plain, key_provider=_crypto_provider(kms_key_arn, raw_key_b64, key_id),
                                       encryption_context={"SDC_FIELD_TYPE": kind, **{str(k): str(v) for k, v in (context or {}).items()}})
    return blob


def decrypt_value(blob: bytes, *, kms_key_arn: str | None = None, raw_key_b64: str | None = None, key_id: str = "", context: dict | None = None):
    """Inverse of encrypt_value; reads Data Collector's ciphertext. The type comes from `SDC_FIELD_TYPE` in the encryption context. The same key is required, and
    any `context` entries you pass must match the ciphertext's."""
    import decimal
    plain, header = _crypto_client().decrypt(source=blob, key_provider=_crypto_provider(kms_key_arn, raw_key_b64, key_id))
    for k, want in (context or {}).items():
        if header.encryption_context.get(str(k)) != str(want):
            raise ValueError(f"encryption context mismatch for {k!r}")
    kind = header.encryption_context.get("SDC_FIELD_TYPE", "STRING")
    text = plain.decode() if kind != "BYTE_ARRAY" else b""
    conv = {"STRING": lambda: text, "INTEGER": lambda: int(text), "LONG": lambda: int(text), "SHORT": lambda: int(text), "BYTE": lambda: int(text), "DOUBLE": lambda: float(text),
            "FLOAT": lambda: float(text), "BOOLEAN": lambda: text == "true", "DECIMAL": lambda: decimal.Decimal(text), "BYTE_ARRAY": lambda: plain}
    if kind not in conv:
        raise ValueError(f"field type {kind} is not supported yet")
    return conv[kind]()


def crypto_fields(run: Run, inst: str, mode: str, fields: list[str], *, kms_key_arn_fn=None, key_fn=None, key_id: str = "", context: dict | None = None,
                  inputs, outputs, on_error="TO_ERROR", pre=(), req=()):
    """Encrypt or decrypt the listed fields in place (mode ENCRYPT | DECRYPT). Encrypted fields hold bytes. Use from a hand-written override or a
    translated stage; keys are read when the first record arrives."""
    def kw():
        return {"kms_key_arn": kms_key_arn_fn() if kms_key_arn_fn else None, "raw_key_b64": key_fn() if key_fn else None, "key_id": key_id, "context": context}

    def one(r: Record):
        for f in fields:
            v = get_path(r.value, f, _MISSING)
            if v is _MISSING or v is None:
                continue
            set_path(r.value, f, encrypt_value(v, **kw()) if mode == "ENCRYPT" else decrypt_value(v, **kw()))
        return r
    _map_records(run, inst, one, inputs=inputs, outputs=outputs, on_error=on_error, pre=pre, req=req)


_HASHES = {"MD5": "md5", "SHA1": "sha1", "SHA256": "sha256", "SHA512": "sha512"}


def field_hasher(run: Run, inst: str, rules, *, targets=(), inputs, outputs, on_error="TO_ERROR", pre=(), req=()):
    """Field Hasher. rules = [([paths], HASH_TYPE)] hash in place; targets = [([paths], HASH_TYPE, target_field, header_attribute)] hash the listed fields together into a field or
    header attribute. The hash is the lower-case hex of the algorithm over the value's native bytes (integers little-endian, strings UTF-8). Maps and lists are hashed element by element."""
    import hashlib

    import struct

    def raw(v) -> bytes:
        """Data Collector hashes a field's native bytes: integers as 4 (or, past 32 bits, 8) little-endian bytes, doubles as 8, strings as UTF-8 (checked against 3.18.1)."""
        if isinstance(v, bool):
            return bytes([1 if v else 0])
        if isinstance(v, int):
            return struct.pack("<i", v) if -2**31 <= v < 2**31 else struct.pack("<q", v)
        if isinstance(v, float):
            return struct.pack("<d", v)
        if isinstance(v, (bytes, bytearray)):
            return bytes(v)
        return str(v).encode()

    def h(t, v):
        return hashlib.new(_HASHES[t], raw(v)).hexdigest()

    def walk(t, v):
        if isinstance(v, dict):
            return {k: walk(t, x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(t, x) for x in v]
        return None if v is None else h(t, v)

    def one(r: Record):
        for paths, t in rules:
            for p in paths:
                v = get_path(r.value, p, _MISSING)
                if v is not _MISSING:
                    set_path(r.value, p, walk(t, v))
        for paths, t, field, header in targets:
            joined = b"".join(raw(get_path(r.value, p, "")) for p in paths)
            hx = hashlib.new(_HASHES[t], joined).hexdigest()
            if field:
                set_path(r.value, field, hx)
            if header:
                r.attrs[header] = hx
        return r
    _map_records(run, inst, one, inputs=inputs, outputs=outputs, on_error=on_error, pre=pre, req=req)


# ---- Elasticsearch, MongoDB, HTTP origin (connections come from the registry's resources) --------------------------------------------------
def es_origin(run: Run, inst: str, hosts: str, index: str, query: str, offset_field: str, initial_offset: str, batch: int, *, outputs, events=()):
    """Elasticsearch origin as a micro-batch. `${OFFSET}` in the query body is replaced by the stored offset (raw text, as StreamSets does); the query repeats
    until it returns no hits, and the last hit's `offset_field` is stored. Connection: the registry's ElasticsearchResource
    (credentials from SDC_SECRET_<STAGE>_API_KEY, or _USERNAME and _PASSWORD)."""
    def gen():
        if run.dry:
            run.log(f"[dry run] {inst}: would query index {index}")
            return
        from dagster_community_components.resources.elasticsearch_resource.component import ElasticsearchResource
        client = ElasticsearchResource(hosts=hosts, api_key=stage_secret(inst, "API_KEY"), username=stage_secret(inst, "USERNAME"),
                                       password=stage_secret(inst, "PASSWORD")).get_client()
        offset = str(run.offsets_in.get(inst, initial_offset))
        while True:
            body = json.loads(query.replace("${OFFSET}", offset))
            hits = client.search(index=index, body=body, size=batch)["hits"]["hits"]
            if not hits:
                break
            yield {outputs[0]: [Record(dict(h["_source"])) for h in hits]}
            if not offset_field or "${OFFSET}" not in query:
                break
            offset = str(get_path(hits[-1]["_source"], "/" + offset_field.strip("/")))
            run.offsets_out[inst] = offset
    run.add(Stage(inst, gen=gen, outputs=outputs, events=events))


def es_target(run: Run, inst: str, hosts_fn, index_fn, *, inputs, on_error="TO_ERROR", pre=(), req=()):
    """Elasticsearch destination: bulk-indexes each record into `index_fn(record)`. The stage speaks the transport protocol (port 9300); this uses
    the HTTP API, so the hosts come from `SDC_ES_URL` (or the stage's URIs) and must point at the HTTP port."""
    def go(recs):
        if run.dry:
            run.log(f"[dry run] {inst}: would index {len(recs)} record(s)")
            return {}
        from dagster_community_components.resources.elasticsearch_resource.component import ElasticsearchResource
        from elasticsearch.helpers import bulk
        client = ElasticsearchResource(hosts=os.environ.get("SDC_ES_URL") or hosts_fn(), api_key=stage_secret(inst, "API_KEY"), username=stage_secret(inst, "USERNAME"),
                                       password=stage_secret(inst, "PASSWORD")).get_client()
        bulk(client, ({"_index": index_fn(r), "_source": r.value} for r in recs))
        return {}
    run.add(Stage(inst, go, inputs=inputs, on_error=on_error, pre=pre, req=req))


def mongo_origin(run: Run, inst: str, conn_fn, database: str, collection: str, offset_field: str, offset_type: str, initial_offset: str, batch: int, *, outputs, events=()):
    """MongoDB origin as a micro-batch: documents with `offset_field` greater than the stored offset, in order, until none are left. OBJECTID offsets start
    from an ObjectId built from the initial offset date; DATE and STRING offsets are compared as such. Connection: the registry's MongoDBResource."""
    def gen():
        if run.dry:
            run.log(f"[dry run] {inst}: would read {database}.{collection}")
            return
        from dagster_community_components.resources.mongodb_resource.component import MongoDBResource
        client = MongoDBResource(connection_string=conn_fn(), database=database).get_client()
        coll = client[database][collection]
        import bson

        def start():
            stored = run.offsets_in.get(inst)
            if stored is not None:
                return bson.ObjectId(stored) if offset_type == "OBJECTID" else (_dt.datetime.fromisoformat(stored) if offset_type == "DATE" else stored)
            if offset_type == "OBJECTID":
                return bson.ObjectId.from_datetime(_dt.datetime.strptime(initial_offset, "%Y-%m-%d %H:%M:%S"))
            return _dt.datetime.strptime(initial_offset, "%Y-%m-%d %H:%M:%S") if offset_type == "DATE" else initial_offset
        cur = start()
        while True:
            docs = list(coll.find({offset_field: {"$gt": cur}}).sort(offset_field, 1).limit(batch))
            if not docs:
                break
            cur = docs[-1][offset_field]
            run.offsets_out[inst] = cur.isoformat() if isinstance(cur, _dt.datetime) else str(cur)
            yield {outputs[0]: [Record({k: (str(v) if isinstance(v, bson.ObjectId) else v) for k, v in d.items()}) for d in docs]}
    run.add(Stage(inst, gen=gen, outputs=outputs, events=events))


def http_origin(run: Run, inst: str, url_fn, method: str, auth_fn, fmt: str, header: bool, delimiter: str, *, outputs, events=()):
    """HTTP Client origin in polling mode, as one request per run; the response body is parsed as DELIMITED or JSON."""
    def gen():
        if run.dry:
            run.log(f"[dry run] {inst}: would {method} {str(url_fn()).split('?')[0]}")
            return
        import io
        resp = _http(run, inst, method, url_fn(), auth=auth_fn())
        rows = list(_parse(io.StringIO(resp.text), fmt, header, delimiter)) if fmt != "JSON" else (
            resp.json() if isinstance(resp.json(), list) else [resp.json()])
        yield {outputs[0]: [Record(x if isinstance(x, dict) else {"value": x}) for x in rows]}
    run.add(Stage(inst, gen=gen, outputs=outputs, events=events))


# ---- more processors --------------------------------------------------------------------------------------------------------------------------
def base64_field(run: Run, inst: str, mode: str, src, dst: str, url_safe: bool, *, inputs, outputs, on_error="TO_ERROR", pre=(), req=()):
    """Base64 Field Encoder/Decoder: byte array in, byte array out; a string field is a record error (as in Data Collector). `src` is one path (with `dst`) or, in the
    Data Collector 6.x schema, a list of (origin path, result path) pairs."""
    import base64
    pairs = list(src) if isinstance(src, (list, tuple)) else [(src, dst)]

    def one(r: Record):
        for s, d in pairs:
            v = get_path(r.value, s, _MISSING)
            if v is _MISSING or v is None:
                raise KeyError(f"field {s} is missing")
            if isinstance(v, str):
                raise TypeError(f"field {s} is a string: Base64 works on byte array fields only")      # Data Collector sends such a record to error
            enc, dec = (base64.urlsafe_b64encode, base64.urlsafe_b64decode) if url_safe else (base64.b64encode, base64.b64decode)
            set_path(r.value, d, enc(bytes(v)) if mode == "ENCODE" else dec(bytes(v)))
        return r
    _map_records(run, inst, one, inputs=inputs, outputs=outputs, on_error=on_error, pre=pre, req=req)


def replace_if_null(run: Run, inst: str, rules, *, inputs, outputs, on_error="TO_ERROR", pre=(), req=()):
    """Field Replacer (Value Replacer), replace-if-null: rules = [([paths], new value)]. The new value is set as written (a string)."""
    def one(r: Record):
        for paths, new in rules:
            for p in paths:
                if get_path(r.value, p, _MISSING) is None:
                    set_path(r.value, p, new)
        return r
    _map_records(run, inst, one, inputs=inputs, outputs=outputs, on_error=on_error, pre=pre, req=req)


def list_pivot(run: Run, inst: str, list_path: str, copy_fields: bool, new_path: str | None, *, inputs, outputs, on_error="TO_ERROR", pre=(), req=()):
    """Field Pivoter: one record per element of the list at `list_path`; the element goes to `new_path` (or replaces the list), other fields copied if asked."""
    holder: dict = {}
    each = _each(holder, run, lambda r: _pivot(r, list_path, copy_fields, new_path))
    holder["st"] = _stage(run, inst, lambda recs: {outputs[0]: each(recs)}, inputs=inputs, outputs=outputs, on_error=on_error, pre=pre, req=req)


def _pivot(r: Record, list_path, copy_fields, new_path):
    lst = get_path(r.value, list_path, _MISSING)
    if not isinstance(lst, list):
        raise TypeError(f"field {list_path} is not a list")
    out = []
    for item in lst:
        n = r.copy() if copy_fields else Record({}, dict(r.attrs))
        if copy_fields and not new_path:
            del_path(n.value, list_path)           # Data Collector removes the list field and adds the element at the end of the record
            set_path(n.value, list_path, item)
        else:
            set_path(n.value, new_path or list_path, item)
        out.append(n)
    return out


def dedup(run: Run, inst: str, window: int, compare: list[str] | None, *, inputs, outputs, on_error="TO_ERROR", pre=(), req=()):
    """Record Deduplicator: the first record with a given content (all fields, or the compared fields) goes to the first output; repeats within the last
    `window` records go to the second output (dropped if there is none). The window lives for one run: nothing is remembered across runs."""
    seen: dict = {}

    def go(recs):
        uniq, dup = [], []
        for r in recs:
            key = json.dumps(r.value if compare is None else [get_path(r.value, f) for f in compare], sort_keys=True, default=str)
            if key in seen:
                dup.append(r)
            else:
                seen[key] = True
                uniq.append(r)
                if len(seen) > window:
                    seen.pop(next(iter(seen)))
        out = {outputs[0]: uniq}
        if len(outputs) > 1:
            out[outputs[1]] = dup
        return out
    _stage(run, inst, go, inputs=inputs, outputs=outputs, on_error=on_error, pre=pre, req=req)


def log_parser(run: Run, inst: str, src: str, dst: str, log_mode: str, *, inputs, outputs, on_error="TO_ERROR", pre=(), req=()):
    """Log Parser: parses the string at `src` as Apache Common/Combined Log Format (or the two grok patterns) into a map at `dst` (`/` merges into the record)."""
    def one(r: Record):
        v = get_path(r.value, src, _MISSING)
        if v is _MISSING or v is None:
            raise KeyError(f"field {src} is missing")
        parsed = parse_log(str(v), log_mode)
        if dst in ("/", ""):
            r.value.clear()                  # parsing into the root replaces the record's fields (the original text is gone), as Data Collector does
            r.value.update(parsed)
        else:
            set_path(r.value, dst, parsed)
        return r
    _map_records(run, inst, one, inputs=inputs, outputs=outputs, on_error=on_error, pre=pre, req=req)


def jdbc_lookup(run: Run, inst: str, url_fn, query_fn, mappings, multiple: str, missing: str, *, inputs, outputs, on_error="TO_ERROR", pre=(), req=()):
    """JDBC Lookup: runs `query_fn(record)` (EL substituted into the SQL text, as StreamSets does) and writes columns to fields. mappings = [(column, field)];
    none means every column to a field of the same name. multiple: FIRST_ONLY | SPLIT_INTO_MULTIPLE_RECORDS | ALL_AS_LIST. missing: PASS_RECORD_ON | SEND_TO_ERROR."""
    import sqlalchemy as sa
    holder: dict = {}
    eng: list = []

    def lookup(r: Record):
        if run.dry:
            run.log(f"[dry run] {inst}: would run the lookup query")
            return [r]
        if not eng:
            eng.append(_engine(url_fn()))
        with eng[0].connect() as conn:
            rows = [dict(x) for x in conn.execute(sa.text(query_fn(r))).mappings().all()]
        if not rows:
            if missing == "SEND_TO_ERROR":
                raise LookupError("no rows returned by the lookup query")
            return [r]
        cols = mappings or [(c, "/" + c) for c in rows[0]]
        if multiple == "FIRST_ONLY":
            rows = rows[:1]
        if multiple == "SPLIT_INTO_MULTIPLE_RECORDS":
            out = []
            for row in rows:
                n = r.copy()
                for c, f in cols:
                    set_path(n.value, f, row.get(c))
                out.append(n)
            return out
        for c, f in cols:
            set_path(r.value, f, [row.get(c) for row in rows] if multiple == "ALL_AS_LIST" else rows[0].get(c))
        return [r]
    each = _each(holder, run, lookup)
    holder["st"] = _stage(run, inst, lambda recs: {outputs[0]: each(recs)}, inputs=inputs, outputs=outputs, on_error=on_error, pre=pre, req=req)


def dev_simulator_origin(run: Run, inst: str, directory: str, pattern: str, routes: list, disc_field: str, ts_field: str, ts_out: str, *, delimiter: str, quote: str,
                         outputs, events=()):
    """Dev Simulator origin: replays delimited files (with a header) as records. Each record is routed to the lane whose discriminator value it carries, and
    an event timestamp (run start + the record's relative delay, in ms) is written to `ts_out`. Nothing is paced: the speed-up setting is ignored."""
    def gen():
        if run.dry:
            run.log(f"[dry run] {inst}: would replay {directory}/{pattern}")
            return
        import glob
        anchor = int(_dt.datetime.now(_dt.timezone.utc).timestamp() * 1000)
        for p in sorted(glob.glob(os.path.join(directory, pattern))):
            lanes: dict = {}
            with open(p, newline="") as fh:
                for row in _parse(fh, "DELIMITED", True, delimiter, quote=quote):
                    if ts_out and ts_field:
                        row[ts_out.strip("/")] = anchor + int(float(row.get(ts_field.strip("/"), 0) or 0))
                    lane = next((ln for val, ln in routes if str(row.get(disc_field.strip("/"))) == val), routes[0][1] if len(routes) == 1 else None)
                    if lane is not None:
                        lanes.setdefault(lane, []).append(Record(row))
            if lanes:
                yield lanes
    run.add(Stage(inst, gen=gen, outputs=outputs, events=events))


# ---- Groovy (runs on the JVM) -----------------------------------------------------------------------------------------------------------------
_GROOVY_RUNNER = r'''
import groovy.json.*
class Rec { def value; Map attributes = [:]; String sourceId; String eventType }
class Out { List recs = []; void write(r) { recs << r } }
class Err { List errs = []; void write(r, m = "") { errs << [r, m.toString()] } }
class Log { void info(m, Object... a) { System.err.println(m) }; void warn(m, Object... a) { System.err.println(m) }; void error(m, Object... a) { System.err.println(m) }
            void debug(m, Object... a) {}; void trace(m, Object... a) {} }
class Sdc {
  List records; Out output = new Out(); Err error = new Err(); Map state; Log log = new Log(); List events = []; Map userParams = [:]
  def createRecord(String id) { new Rec(sourceId: id, value: [:]) }
  def createMap(boolean listMap) { [:] }
  def createEvent(String type, int version) { new Rec(eventType: type, value: [:], sourceId: "event") }
  void toEvent(e) { events << e }
}
def cfg = new JsonSlurper().parse(new File(args[0]))
def sdc = new Sdc(state: cfg.state ?: [:], userParams: cfg.userParams ?: [:])
def mk = { m -> new Rec(value: m.value, attributes: m.attributes ?: [:], sourceId: m.sourceId) }
sdc.records = cfg.records.collect(mk)
def out = { r -> [value: r.value, attributes: r.attributes ?: [:], eventType: r.eventType] }
def run = { String file, boolean perRecord ->
  def src = new File(file).text
  if (perRecord) {
    sdc.records.each { rec -> new GroovyShell(new Binding([sdc: sdc, records: sdc.records, output: sdc.output, error: sdc.error, log: sdc.log, state: sdc.state, record: rec])).evaluate(src) }
  } else {
    new GroovyShell(new Binding([sdc: sdc, records: sdc.records, output: sdc.output, error: sdc.error, log: sdc.log, state: sdc.state])).evaluate(src)
  }
}
if (cfg.init) { def saved = sdc.records; sdc.records = []; run(cfg.init, false); sdc.records = saved }
run(cfg.script, cfg.perRecord as boolean)
new File(args[1]).text = JsonOutput.toJson([out: sdc.output.recs.collect(out), err: sdc.error.errs.collect { [out(it[0]), it[1]] }, ev: sdc.events.collect(out), state: sdc.state])
'''


def groovy_processor(run: Run, inst: str, script: str, init: str, mode: str, *, user_params: dict | None = None, inputs, outputs, events=(), on_error="TO_ERROR", pre=(), req=()):
    """Groovy Evaluator run by the real Groovy on the JVM: `groovy` on PATH (or SDC_GROOVY) and a JDK (JAVA_HOME). One process per batch; records cross as JSON.
    The scripting objects are `sdc.records/output/error/log/state/createRecord/createEvent/toEvent` and the globals `records output error log state` (and `record`)."""
    import shutil
    import subprocess
    import tempfile
    state: dict = {}
    holder: dict = {}
    started = [False]

    def call(recs, src, run_init=""):
        exe = os.environ.get("SDC_GROOVY") or shutil.which("groovy")
        if not exe:
            raise SdcCaveat(f"SDC-CAVEAT: {inst}: the Groovy evaluator needs the `groovy` command (a JDK and Apache Groovy): put it on PATH or set SDC_GROOVY")
        with tempfile.TemporaryDirectory() as d:
            payload = [{"value": r.value, "attributes": r.attrs, "sourceId": f"{inst}:{i}"} for i, r in enumerate(recs)]
            open(f"{d}/s.groovy", "w").write(src)
            open(f"{d}/init.groovy", "w").write(run_init)
            open(f"{d}/runner.groovy", "w").write(_GROOVY_RUNNER)
            open(f"{d}/in.json", "w").write(json.dumps({"records": payload, "state": state, "script": f"{d}/s.groovy", "init": f"{d}/init.groovy" if run_init.strip() else None,
                                                         "perRecord": mode == "RECORD", "userParams": user_params or {}}, default=str))
            p = subprocess.run([exe, f"{d}/runner.groovy", f"{d}/in.json", f"{d}/out.json"], capture_output=True, text=True)
            if p.returncode != 0 or not os.path.exists(f"{d}/out.json"):
                raise RuntimeError(f"Groovy script failed: {(p.stderr or p.stdout)[-600:]}")
            return json.load(open(f"{d}/out.json"))

    def go(recs):
        res = call(recs, script, init if not started[0] else "")
        started[0] = True
        state.clear()
        state.update(res["state"] or {})
        out = [Record(x["value"] if isinstance(x["value"], dict) else {}, {k: str(v) for k, v in (x.get("attributes") or {}).items()}) for x in res["out"]]
        for item, msg in res["err"]:
            run._err(holder["st"], Record(item["value"] if isinstance(item["value"], dict) else {}), msg)
        lanes = {outputs[0]: out} if outputs else {}
        if events and res["ev"]:
            lanes[events[0]] = [Record(e["value"] or {}, {}, e.get("eventType")) for e in res["ev"]]
        return lanes
    holder["st"] = _stage(run, inst, go, inputs=inputs, outputs=outputs, events=events, on_error=on_error, pre=pre, req=req)


def xml_flatten(run: Run, inst: str, src: str, delimiter: str | None, field_delim: str, attr_delim: str, ignore_attrs: bool, keep_original: bool, overwrite: bool,
                out_field: str | None, *, inputs, outputs, on_error="TO_ERROR", pre=(), req=()):
    """XML Flattener, as documented: element paths joined with `field_delim`, attributes with `attr_delim`, repeated sibling elements indexed `name(0)`,
    one record per `delimiter` element (rooted at that element) or one record for the document. Namespaces are dropped from names."""
    import xml.etree.ElementTree as ET

    def local(tag):
        return tag.rsplit("}", 1)[-1]

    def flat(el, prefix, out):
        if not ignore_attrs:
            for k, v in el.attrib.items():
                out[f"{prefix}{attr_delim}{local(k)}"] = v
        kids = list(el)
        if not kids:
            if (el.text or "").strip():
                out[prefix] = el.text.strip()
            return
        counts: dict = {}
        for k in kids:
            counts[local(k.tag)] = counts.get(local(k.tag), 0) + 1
        seen: dict = {}
        for k in kids:
            t = local(k.tag)
            i = seen.get(t, 0)
            seen[t] = i + 1
            flat(k, f"{prefix}{field_delim}{t}" + (f"({i})" if counts[t] > 1 else ""), out)

    def one(r: Record):
        text = get_path(r.value, src, _MISSING)
        if text is _MISSING or text is None:
            raise KeyError(f"field {src} is missing")
        root = ET.fromstring(text if isinstance(text, (str, bytes)) else str(text))
        docs = [(local(el.tag), el) for el in root.findall(f".//{delimiter}")] if delimiter else [(local(root.tag), root)]
        if delimiter and not docs:
            raise ValueError(f"no <{delimiter}> element in the document")
        out_recs = []
        for name, el in docs:
            flatmap: dict = {}
            flat(el, name, flatmap)
            n = r.copy() if keep_original else Record({}, dict(r.attrs))
            if out_field:
                set_path(n.value, out_field, flatmap)
            else:
                for k, v in flatmap.items():
                    if k in n.value and not overwrite:
                        raise KeyError(f"field {k} already exists")
                    n.value[k] = v
            out_recs.append(n)
        return out_recs
    holder: dict = {}
    each = _each(holder, run, one)
    holder["st"] = _stage(run, inst, lambda recs: {outputs[0]: each(recs)}, inputs=inputs, outputs=outputs, on_error=on_error, pre=pre, req=req)


# ---- more origins and destinations -----------------------------------------------------------------------------------------------------------
def spooldir_origin(run: Run, inst: str, directory: str, pattern: str, fmt: str, *, header: bool, delimiter: str, outputs, events, batch: int = 1000,
                    order: str = "LEXICOGRAPHICAL", log_mode: str = ""):
    """Directory origin as one batch: reads the files that match when the run starts, in order, then stops (StreamSets keeps polling).
    Files already read by an earlier run are skipped (the last file's order key is stored as an offset)."""
    def gen():
        if run.dry:
            run.log(f"[dry run] {inst}: would read {directory}/{pattern}")
            return
        import glob
        files = glob.glob(os.path.join(directory, pattern))
        key = (lambda p: (os.path.getmtime(p), p)) if order == "TIMESTAMP" else (lambda p: p)
        mark = run.offsets_in.get(inst)
        for p in sorted(files, key=key):
            k = key(p)
            if mark is not None and (list(k) if isinstance(k, tuple) else k) <= mark:
                continue
            n, meta = 0, {"file": p, "filename": os.path.basename(p), "Name": os.path.basename(p)}
            import gzip
            with (gzip.open(p, "rt", newline="") if p.endswith(".gz") else open(p, newline="")) as fh:
                buf = []
                for row in _parse(fh, fmt, header, delimiter, log_mode):
                    buf.append(Record(row, dict(meta)))
                    n += 1
                    if len(buf) >= batch:
                        yield {outputs[0]: buf}
                        buf = []
                if buf:
                    yield {outputs[0]: buf}
            run.offsets_out[inst] = list(k) if isinstance(k, tuple) else k
            if events:
                yield {events[0]: [Record({"filepath": p, "filename": meta["filename"], "record-count": n}, dict(meta), "finished-file")]}
    run.add(Stage(inst, gen=gen, outputs=outputs, events=events))


def salesforce_origin(run: Run, inst: str, creds_fn, soql_fn, offset_col: str, initial_offset: str, batch: int, *, outputs, events=()):
    """Salesforce origin (REST query). `${OFFSET}` in the query is replaced by the stored offset; the query repeats until it returns no rows,
    and the last `offset_col` value is stored for the next run. Records carry `salesforce.sobjectType`."""
    def gen():
        if run.dry:
            run.log(f"[dry run] {inst}: would run the SOQL query")
            return
        import simple_salesforce as ss
        user, pw, token, domain = creds_fn()
        sf = ss.Salesforce(username=user, password=pw, security_token=token, domain=domain)
        soql = soql_fn()
        offset = run.offsets_in.get(inst, initial_offset)
        while True:
            q = soql.replace("${OFFSET}", str(offset))
            buf, last = [], None
            for row in sf.query_all_iter(q):
                attrs = {"salesforce.sobjectType": (row.get("attributes") or {}).get("type", "")}
                value = {k: v for k, v in row.items() if k != "attributes"}
                buf.append(Record(value, attrs))
                last = value
                if len(buf) >= batch:
                    yield {outputs[0]: buf}
                    buf = []
            if buf:
                yield {outputs[0]: buf}
            if not last or not offset_col or "${OFFSET}" not in soql or last.get(offset_col) is None:
                break
            offset = last[offset_col]
            run.offsets_out[inst] = offset
    run.add(Stage(inst, gen=gen, outputs=outputs, events=events))


def delta_url(inst: str, jdbc_url: str, token: str = "") -> str:
    """SQLAlchemy URL for a Databricks JDBC URL (needs databricks-sqlalchemy); SDC_SQLALCHEMY_URL_<STAGE> overrides it."""
    env = os.environ.get(f"SDC_SQLALCHEMY_URL_{re.sub(r'[^A-Za-z0-9]', '_', inst).upper()}")
    if env:
        return env
    m = re.match(r"^jdbc:(?:databricks|spark)://([^:/;]+)(?::\d+)?/?([^;]*)(.*)$", jdbc_url or "")
    if not m:
        raise SdcCaveat(f"SDC-CAVEAT: {inst}: cannot derive a SQLAlchemy URL from {jdbc_url!r}; set SDC_SQLALCHEMY_URL_{re.sub(r'[^A-Za-z0-9]', '_', inst).upper()}")
    http = re.search(r"httpPath=([^;]+)", m.group(3))
    return f"databricks://token:{token}@{m.group(1)}?http_path={http.group(1) if http else ''}"


_SA_TYPES = {bool: "Boolean", int: "BigInteger", float: "Float"}


def delta_target(run: Run, inst: str, url_fn, table_fn, *, auto_create: bool, drift: bool, keys_fn=None, cdc: bool = False, inputs, on_error="TO_ERROR", pre=(), req=()):
    """Databricks Delta Lake destination via SQL: creates the table from the first record when missing, adds columns for new fields when drift is on,
    and in CDC mode applies `sdc.operation.type` (1 insert, 2 delete, 3 update, 4 upsert) by key as delete-then-insert."""
    import decimal
    import sqlalchemy as sa

    def sa_type(v):
        if isinstance(v, decimal.Decimal):
            return sa.Numeric(38, 10)
        if isinstance(v, __import__("datetime").datetime):
            return sa.DateTime()
        if isinstance(v, __import__("datetime").date):
            return sa.Date()
        return getattr(sa, _SA_TYPES.get(type(v), "Text"))()

    def go(recs):
        by: dict = {}
        for r in recs:
            by.setdefault(table_fn(r), []).append(r)
        for full, rs in by.items():
            if run.dry:
                run.log(f"[dry run] {inst}: would write {len(rs)} record(s) to {full}")
                continue
            schema, _, name = full.rpartition(".")
            eng = _engine(url_fn())
            insp = sa.inspect(eng)
            cols = {}
            for r in rs:
                for k, v in r.value.items():
                    if v is not None and k not in cols:
                        cols[k] = v
                    cols.setdefault(k, None)
            if not insp.has_table(name, schema=schema or None):
                if not auto_create:
                    raise RuntimeError(f"table {full} does not exist and table auto create is off")
                with eng.begin() as conn:
                    sa.Table(name, sa.MetaData(), *[sa.Column(k, sa_type(v)) for k, v in cols.items()], schema=schema or None).create(conn)
            elif drift:
                have = {c["name"] for c in insp.get_columns(name, schema=schema or None)}
                with eng.begin() as conn:
                    for k, v in cols.items():
                        if k not in have:
                            conn.execute(sa.text(f'ALTER TABLE {full} ADD COLUMN {k} {sa_type(v).compile(dialect=eng.dialect)}'))
            tbl = sa.Table(name, sa.MetaData(), autoload_with=eng, schema=schema or None)
            keys = keys_fn(full) if (cdc and keys_fn) else []
            if cdc and not keys:
                raise RuntimeError(f"CDC mode needs key columns for {full} (config.data.tableKeyBeans); none match")
            with eng.begin() as conn:
                for r in rs:
                    row = {k: v for k, v in r.value.items() if k in tbl.c}
                    op = int(r.attrs.get("sdc.operation.type", 1)) if cdc else 1
                    if cdc and op in (2, 3, 4):
                        cond = sa.and_(*[tbl.c[k] == r.value.get(k) for k in keys])
                        conn.execute(tbl.delete().where(cond))
                    if op in (1, 3, 4):
                        conn.execute(tbl.insert(), [row])
        return {}
    run.add(Stage(inst, go, inputs=inputs, on_error=on_error, pre=pre, req=req))


def local_fs_target(run: Run, inst: str, dir_fn, prefix: str, fmt: str, *, header: bool, delimiter: str, text_path: str = "/text", inputs, on_error="TO_ERROR", pre=(), req=()):
    """Local FS destination: one file per directory per run, named `<prefix>_<uuid>`; DELIMITED, JSON (one object per line) and TEXT."""
    import csv
    files: dict = {}

    def close():
        for fh, _ in files.values():
            fh.close()
    run.closers.append(close)

    def go(recs):
        for r in recs:
            d = str(dir_fn(r))
            if run.dry:
                run.log(f"[dry run] {inst}: would write to {d}")
                continue
            if d not in files:
                os.makedirs(d, exist_ok=True)
                files[d] = (open(os.path.join(d, f"{prefix}_{_uuid.uuid4()}"), "w", newline=""), {})
            fh, st = files[d]
            if fmt == "SDC_JSON":
                fh.write(json.dumps({"value": r.value, "attributes": r.attrs}, default=str) + "\n")
            elif fmt == "JSON":
                fh.write(json.dumps(r.value, default=str) + "\n")
            elif fmt == "TEXT":
                fh.write(str(get_path(r.value, text_path, "")) + "\n")
            else:
                w = csv.writer(fh, delimiter=delimiter)
                if "cols" not in st:
                    st["cols"] = list(r.value)
                    if header:
                        w.writerow(st["cols"])
                w.writerow([r.value.get(c) for c in st["cols"]])
        return {}
    run.add(Stage(inst, go, inputs=inputs, on_error=on_error, pre=pre, req=req))


def finish(run: Run) -> dict:
    stats = run.execute()
    return stats
