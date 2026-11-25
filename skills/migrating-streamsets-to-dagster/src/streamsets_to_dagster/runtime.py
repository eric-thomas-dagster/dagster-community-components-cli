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

    def copy(self) -> "Record":
        return Record(json.loads(json.dumps(self.value, default=str)), dict(self.attrs), self.event)


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


# ---- engine --------------------------------------------------------------------------------------------------------------------------------
class Stage:
    """A node: `fn(records) -> {lane: [records]}` for processors/targets; origins set `gen` instead."""

    def __init__(self, instance, fn=None, gen=None, inputs=(), outputs=(), events=(), on_error="TO_ERROR", pre=()):
        self.instance, self.fn, self.gen = instance, fn, gen
        self.inputs, self.outputs, self.events = list(inputs), list(outputs), list(events)
        self.on_error, self.pre = on_error, list(pre)


class Run:
    def __init__(self, context, name: str, C: dict, offsets: dict | None = None):
        self.ctx, self.name, self.C = context, name, C
        self.offsets_in, self.offsets_out = dict(offsets or {}), dict(offsets or {})
        self.stages: list[Stage] = []
        self.counts = {"records_in": {}, "records_out": {}, "errors": {}}
        self.dry = dry_run()
        self.stubs: list[tuple[str, str]] = []

    def add(self, stage: Stage) -> None:
        self.stages.append(stage)

    def log(self, msg: str) -> None:
        (self.ctx.log.info if self.ctx is not None else print)(msg)

    def _precond(self, st: Stage, recs: list[Record]) -> list[Record]:
        if not st.pre:
            return recs
        keep = []
        for r in recs:
            if all(fx_truthy(p(r)) for p in st.pre):
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


def expression(run: Run, inst: str, fields, headers, *, inputs, outputs, on_error="TO_ERROR", pre=()):
    """Expression Evaluator: fields = [(path, fn)], headers = [(attribute, fn)]. Header expressions see the record before field changes (as in SDC)."""
    holder: dict = {}

    def one(r: Record):
        for name, fn in headers:
            r.attrs[name] = None if (v := fn(r)) is None else str(v)
        for path, fn in fields:
            set_path(r.value, path, fn(r))
        return r
    each = _each(holder, run, one)
    st = Stage(inst, lambda recs: {outputs[0]: each(recs)} if outputs else {}, inputs=inputs, outputs=outputs, on_error=on_error, pre=pre)
    holder["st"] = st
    run.add(st)


def field_filter(run: Run, inst: str, op: str, fields: list[str], *, inputs, outputs, on_error="TO_ERROR", pre=()):
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
    st = Stage(inst, lambda recs: {outputs[0]: each(recs)}, inputs=inputs, outputs=outputs, on_error=on_error, pre=pre)
    holder["st"] = st
    run.add(st)


def field_flattener(run: Run, inst: str, fields: list[str], sep: str, target: str, remove: bool, *, inputs, outputs, on_error="TO_ERROR", pre=()):
    def flat(v, prefix):
        if isinstance(v, dict):
            for k, x in v.items():
                yield from flat(x, f"{prefix}{sep}{k}" if prefix else str(k))
        else:
            yield prefix, v

    def one(r: Record):
        for f in fields:
            sub = get_path(r.value, f, _MISSING)
            if sub is _MISSING:
                continue
            if remove:
                del_path(r.value, f)
            base = f.strip("/").replace("/", sep)
            for k, x in flat(sub, base):
                set_path(r.value, (target.rstrip("/") + "/" + k) if target not in ("", "/") else "/" + k, x)
        return r
    holder: dict = {}
    each = _each(holder, run, one)
    st = Stage(inst, lambda recs: {outputs[0]: each(recs)}, inputs=inputs, outputs=outputs, on_error=on_error, pre=pre)
    holder["st"] = st
    run.add(st)


def selector(run: Run, inst: str, preds, *, inputs, outputs, on_error="TO_ERROR", pre=()):
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
    run.add(Stage(inst, go, inputs=inputs, outputs=outputs, on_error=on_error, pre=pre))


def field_mapper(run: Run, inst: str, operate_on: str, mapping, cond, *, inputs, outputs, on_error="TO_ERROR", pre=()):
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
    st = Stage(inst, lambda recs: {outputs[0]: each(recs)}, inputs=inputs, outputs=outputs, on_error=on_error, pre=pre)
    holder["st"] = st
    run.add(st)


def null_target(run: Run, inst: str, *, inputs, on_error="TO_ERROR", pre=()):
    run.add(Stage(inst, lambda recs: {}, inputs=inputs, on_error=on_error, pre=pre))


def pipeline_finisher(run: Run, inst: str, *, inputs, on_error="TO_ERROR", pre=()):
    """Pipeline Finisher: in a batch run the pipeline ends when the origins are exhausted, so receiving the record changes nothing here."""
    run.add(Stage(inst, lambda recs: {}, inputs=inputs, on_error=on_error, pre=pre))


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


def jdbc_target(run: Run, inst: str, url_fn, table_fn, op: str, *, inputs, on_error="TO_ERROR", pre=(), key_columns=()):
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
    run.add(Stage(inst, go, inputs=inputs, on_error=on_error, pre=pre))


def jdbc_query_executor(run: Run, inst: str, url_fn, queries, *, inputs, on_error="TO_ERROR", pre=()):
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
    run.add(Stage(inst, go, inputs=inputs, on_error=on_error, pre=pre))


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


def _parse(fh, fmt: str, header: bool, delimiter: str):
    import csv
    if fmt == "DELIMITED":
        rd = csv.reader(fh, delimiter=delimiter)
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


def finish(run: Run) -> dict:
    stats = run.execute()
    return stats
