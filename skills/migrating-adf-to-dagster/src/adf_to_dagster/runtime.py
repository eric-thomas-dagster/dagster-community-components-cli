"""Runtime for generated ADF-to-Dagster projects (copied into each project as <pkg>/runtime.py).

Everything here is deliberately plain Python: the pipeline's activity graph is generated code that calls these helpers.
With ADF_DRY_RUN=1 nothing leaves the process: web calls and SQL are logged and return empty results, waits do not sleep, loops run once
at most, and a stub (adf_caveat) still fails, so a dry run proves structure and wiring, not behavior.
"""
from __future__ import annotations

import base64 as _b64
import json as _json
import os
import re
import threading
import time
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable


def dry_run() -> bool:
    return os.environ.get("ADF_DRY_RUN") == "1"


class AdfCaveat(Exception):
    """A stub: this part of the source pipeline has no automatic translation."""


class AdfFail(Exception):
    """A Fail activity."""


class AdfExprError(Exception):
    """An expression could not be evaluated (missing parameter, variable, property...)."""


def adf_caveat(reason: str):
    raise AdfCaveat(f"ADF-CAVEAT: {reason}")


class Dry:
    """Stand-in for a value that only exists on a real run (a Lookup result in a dry run). Absorbs access, is falsy, iterates empty."""

    def __getitem__(self, k): return self
    def __getattr__(self, k): return self
    def __iter__(self): return iter(())
    def __bool__(self): return False
    def __str__(self): return "<dry>"
    def __int__(self): return 0
    def __float__(self): return 0.0
    def __len__(self): return 0
    def __eq__(self, o): return False
    def __hash__(self): return 0
    def __lt__(self, o): return False
    __gt__ = __le__ = __ge__ = __lt__
    def __add__(self, o): return self
    __radd__ = __sub__ = __rsub__ = __mul__ = __rmul__ = __truediv__ = __rtruediv__ = __floordiv__ = __rfloordiv__ = __mod__ = __rmod__ = __add__


DRY = Dry()


class Out(dict):
    """An activity's output: missing properties are an error on a real run and a Dry value in a dry run."""

    def __missing__(self, k):
        if dry_run():
            return DRY
        raise AdfExprError(f"activity output has no property {k!r}")


# ---------------------------------------------------------------------------------------------------------------------------------------------
# expression functions (ADF names, python spelling)
# ---------------------------------------------------------------------------------------------------------------------------------------------

_US_INPUTS = ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")
_STD = {"d": "M/d/yyyy", "D": "dddd, MMMM d, yyyy", "f": "dddd, MMMM d, yyyy h:mm tt", "F": "dddd, MMMM d, yyyy h:mm:ss tt", "g": "M/d/yyyy h:mm tt",
        "G": "M/d/yyyy h:mm:ss tt", "t": "h:mm tt", "T": "h:mm:ss tt", "m": "MMMM d", "M": "MMMM d", "y": "MMMM yyyy", "Y": "MMMM yyyy",
        "o": "yyyy-MM-ddTHH:mm:ss.fffffffK", "O": "yyyy-MM-ddTHH:mm:ss.fffffffK", "s": "yyyy-MM-ddTHH:mm:ss", "u": "yyyy-MM-dd HH:mm:ssZ",
        "R": "ddd, dd MMM yyyy HH:mm:ss 'GMT'", "r": "ddd, dd MMM yyyy HH:mm:ss 'GMT'"}
_TOKEN = re.compile(r"y{1,5}|M{1,4}|d{1,4}|H{1,2}|h{1,2}|m{1,2}|s{1,2}|f{1,7}|F{1,7}|t{1,2}|K|z{1,3}|'[^']*'|\"[^\"]*\"|\\.|.", re.S)


def _parse_dt(s) -> datetime:
    """A timestamp as ADF accepts it: ISO 8601 (with or without a zone) or the US forms such as 03/15/2018 12:00:00. No zone means UTC."""
    if isinstance(s, datetime):
        return s
    if dry_run() and (isinstance(s, Dry) or not str(s).strip()):
        return datetime(1970, 1, 1, tzinfo=timezone.utc)      # a value that only exists on a real run: a fixed date keeps the dry run going
    t = str(s).strip().replace("Z", "+00:00")
    t = re.sub(r"(\.\d{6})\d+", r"\1", t)
    try:
        d = datetime.fromisoformat(t)
    except ValueError:
        for f in _US_INPUTS:
            try:
                d = datetime.strptime(str(s).strip(), f)
                break
            except ValueError:
                continue
        else:
            raise AdfExprError(f"cannot read {s!r} as a timestamp")
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _net_format(d: datetime, fmt: str) -> str:
    """Format a datetime with a .NET custom or standard format string (en-US), as formatDateTime and the other date functions do."""
    if len(fmt) == 1 and fmt in _STD:
        fmt = _STD[fmt]
        if fmt.endswith("Z") and d.tzinfo is not None and fmt != "yyyy-MM-ddTHH:mm:ss.fffffffK":
            d = d.astimezone(timezone.utc)
    out = []
    for m in _TOKEN.finditer(fmt):
        t = m.group(0)
        c = t[0]
        if c in "'\"":
            out.append(t[1:-1])
        elif c == "\\" and len(t) == 2:
            out.append(t[1])
        elif c == "y":
            out.append(f"{d.year:04d}" if len(t) >= 4 else (f"{d.year % 100:02d}" if len(t) == 2 else str(d.year % 100)))
        elif c == "M":
            out.append({1: str(d.month), 2: f"{d.month:02d}", 3: d.strftime("%b"), 4: d.strftime("%B")}[len(t)])
        elif c == "d":
            out.append({1: str(d.day), 2: f"{d.day:02d}", 3: d.strftime("%a"), 4: d.strftime("%A")}[len(t)])
        elif c == "H":
            out.append(f"{d.hour:02d}" if len(t) == 2 else str(d.hour))
        elif c == "h":
            h12 = d.hour % 12 or 12
            out.append(f"{h12:02d}" if len(t) == 2 else str(h12))
        elif c == "m":
            out.append(f"{d.minute:02d}" if len(t) == 2 else str(d.minute))
        elif c == "s":
            out.append(f"{d.second:02d}" if len(t) == 2 else str(d.second))
        elif c == "f":
            out.append(f"{d.microsecond:06d}0"[:len(t)])
        elif c == "F":
            out.append(f"{d.microsecond:06d}0"[:len(t)].rstrip("0"))
        elif c == "t":
            out.append(("AM" if d.hour < 12 else "PM")[:len(t)])
        elif c == "K":
            off = d.utcoffset()
            out.append("" if off is None else ("Z" if off == timedelta(0) else f"{'+' if off > timedelta(0) else '-'}{abs(off) // timedelta(hours=1):02d}:{abs(off) // timedelta(minutes=1) % 60:02d}"))
        elif c == "z":
            off = d.utcoffset() or timedelta(0)
            sign, hrs, mins = ("+" if off >= timedelta(0) else "-"), abs(off) // timedelta(hours=1), abs(off) // timedelta(minutes=1) % 60
            out.append({1: f"{sign}{hrs}", 2: f"{sign}{hrs:02d}", 3: f"{sign}{hrs:02d}:{mins:02d}"}[len(t)])
        else:
            out.append(t)
    return "".join(out)


def _iso(d: datetime) -> str:
    return _net_format(d, "o")


_TZ = None


def _zone(name: str):
    """A Windows or IANA zone name as a tzinfo; an unknown name is an error."""
    from zoneinfo import ZoneInfo
    from .tzmap import WINDOWS_TZ
    try:
        return ZoneInfo(WINDOWS_TZ.get(str(name), str(name)))
    except Exception as e:                    # noqa: BLE001
        raise AdfExprError(f"unknown time zone {name!r}") from e


def _add_months(d: datetime, n: int) -> datetime:
    import calendar
    m0 = d.month - 1 + n
    y, m = d.year + m0 // 12, m0 % 12 + 1
    return d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1]))


_UNITS = {"second": "seconds", "minute": "minutes", "hour": "hours", "day": "days", "week": "weeks"}


def _shift(d: datetime, n: int, unit) -> datetime:
    u = str(unit).lower()
    if u in _UNITS:
        return d + timedelta(**{_UNITS[u]: n})
    if u == "month":
        return _add_months(d, n)
    if u == "year":
        return _add_months(d, 12 * n)
    raise AdfExprError(f"time unit {unit!r} is not supported")


def _truthy(v) -> bool:
    return bool(v)


class fx:
    @staticmethod
    def get(o, k, opt=False):
        if isinstance(o, Dry):
            return DRY
        if isinstance(o, dict):
            if k in o:
                return o[k]
            for kk in o:
                if isinstance(kk, str) and isinstance(k, str) and kk.lower() == k.lower():
                    return o[kk]
            if opt:
                return None
            if dry_run():
                return DRY
            raise AdfExprError(f"no property {k!r}")
        if isinstance(o, (list, tuple)) and isinstance(k, int):
            try:
                return o[k]
            except IndexError:
                if dry_run():
                    return DRY               # a list that is empty only because the dry run has no data
                raise
        if o is None and opt:
            return None
        raise AdfExprError(f"cannot read {k!r} of {type(o).__name__}")

    @staticmethod
    def translator_mapping(t):
        """A Copy activity's translator (a dict, or JSON text as metadata-driven pipelines pass it) as {source column: sink column}, or None for the default mapping.
        Sources are named, or by 1-based ordinal (which reads as Prop_<n-1>); hierarchical path mappings and sink ordinals are errors here."""
        if isinstance(t, Dry) or t in (None, ""):
            return None
        if isinstance(t, str):
            t = _json.loads(t) if t.strip() else None
        if not isinstance(t, dict) or not t.get("mappings"):
            return None                                        # typeConversion settings only: the default mapping applies
        out = {}
        for m in t["mappings"]:
            src, snk = (m or {}).get("source") or {}, (m or {}).get("sink") or {}
            if "path" in src or "path" in snk:
                raise AdfExprError("a path mapping needs a hierarchical source (REST or JSON), not a table or delimited file")
            if "ordinal" in snk or not snk.get("name"):
                raise AdfExprError("a mapping to a sink column by ordinal is not supported")
            key = src.get("name") if src.get("name") else (f"Prop_{int(src['ordinal']) - 1}" if "ordinal" in src else None)
            if key is None:
                raise AdfExprError(f"mapping {m!r} names no source column")
            out[key] = snk["name"]
        return out

    @staticmethod
    def envvar(name):
        """An environment variable the generated code needs (a base url the export did not hold); empty in a dry run."""
        v = os.environ.get(name)
        if v is None:
            if dry_run():
                return "https://dry-run.invalid"
            raise AdfExprError(f"set the environment variable {name}")
        return v.rstrip("/")

    @staticmethod
    def interp(parts): return "".join(fx.string(p) for p in parts)

    @staticmethod
    def string(v):
        if isinstance(v, bool): return "true" if v else "false"
        if v is None: return ""
        if isinstance(v, (dict, list)): return _json.dumps(v, default=str)       # a Dry stand-in inside a dry run's data prints as <dry>
        return str(v)

    @staticmethod
    def concat(*a):
        if a and all(isinstance(x, list) for x in a):
            return [i for x in a for i in x]
        return "".join(fx.string(x) for x in a)

    @staticmethod
    def equals(a, b): return a == b
    @staticmethod
    def not_(a): return not a
    @staticmethod
    def and_(*a): return all(a)
    @staticmethod
    def or_(*a): return any(a)
    @staticmethod
    def if_(c, a, b): return a if c else b
    @staticmethod
    def int_(v):
        try:
            return int(float(v)) if isinstance(v, str) and "." in v else int(v)
        except (ValueError, TypeError):
            if dry_run():
                return 0                       # a value that only exists on a real run
            raise
    @staticmethod
    def float_(v):
        try:
            return float(v)
        except (ValueError, TypeError):
            if dry_run():
                return 0.0
            raise
    @staticmethod
    def bool_(v): return (v.strip().lower() == "true") if isinstance(v, str) else bool(v)

    @staticmethod
    def contains(c, v):
        if isinstance(c, str): return str(v) in c                  # documented as case-sensitive
        return v in c

    @staticmethod
    def replace(s, a, b): return str(s).replace(a, b)
    @staticmethod
    def split(s, sep): return str(s).split(sep) if sep else list(str(s))
    @staticmethod
    def join(a, sep): return sep.join(fx.string(x) for x in a)
    @staticmethod
    def length(c): return len(c)
    @staticmethod
    def empty(c): return c is None or len(c) == 0
    @staticmethod
    def first(c): return c[0] if len(c) else None
    @staticmethod
    def last(c): return c[-1] if len(c) else None
    @staticmethod
    def add(a, b): return a + b
    @staticmethod
    def sub(a, b): return a - b
    @staticmethod
    def mul(a, b): return a * b
    @staticmethod
    def div(a, b):
        if dry_run() and (isinstance(a, Dry) or isinstance(b, Dry) or not b):
            return 0
        return a // b if isinstance(a, int) and isinstance(b, int) else a / b
    @staticmethod
    def mod(a, b):
        if dry_run() and (isinstance(a, Dry) or isinstance(b, Dry) or not b):
            return 0
        return a % b
    @staticmethod
    def greater(a, b): return a > b
    @staticmethod
    def greaterorequals(a, b): return a >= b
    @staticmethod
    def less(a, b): return a < b
    @staticmethod
    def lessorequals(a, b): return a <= b
    @staticmethod
    def startswith(s, p): return str(s).lower().startswith(str(p).lower())
    @staticmethod
    def endswith(s, p): return str(s).lower().endswith(str(p).lower())
    @staticmethod
    def tolower(s): return str(s).lower()
    @staticmethod
    def toupper(s): return str(s).upper()
    @staticmethod
    def trim(s): return str(s).strip()
    @staticmethod
    def substring(s, start, length=None): return str(s)[start:] if length is None else str(s)[start:start + length]
    @staticmethod
    def indexof(s, v): return str(s).lower().find(str(v).lower())
    @staticmethod
    def lastindexof(s, v): return str(s).lower().rfind(str(v).lower())
    @staticmethod
    def coalesce(*a): return next((x for x in a if x is not None), None)
    @staticmethod
    def json_(v):
        try:
            return _json.loads(v) if isinstance(v, str) else v
        except (ValueError, TypeError):
            if dry_run():
                return DRY                       # a value that only exists on a real run
            raise
    @staticmethod
    def createarray(*a): return list(a)
    @staticmethod
    def array(v): return [v]
    @staticmethod
    def union(*a):
        if all(isinstance(x, dict) for x in a):
            out = {}
            for x in a: out.update(x)
            return out
        out = []
        for x in a:
            for i in x:
                if i not in out: out.append(i)
        return out
    @staticmethod
    def intersection(*a):
        first = a[0]
        if isinstance(first, dict):
            return {k: v for k, v in first.items() if all(k in o and o[k] == v for o in a[1:])}
        out = []
        for x in first:
            if all(x in o for o in a[1:]) and x not in out:
                out.append(x)
        return out
    @staticmethod
    def rand(lo, hi):
        import random
        return random.randrange(int(lo), int(hi))                    # the maximum is excluded
    @staticmethod
    def ticks(v):
        d = _parse_dt(v).astimezone(timezone.utc).replace(tzinfo=None)
        delta = d - datetime(1, 1, 1)
        return (delta.days * 86400 + delta.seconds) * 10_000_000 + delta.microseconds * 10
    @staticmethod
    def take(c, n): return c[:n]
    @staticmethod
    def skip(c, n): return c[n:]
    @staticmethod
    def base64(s): return _b64.b64encode(str(s).encode()).decode()
    @staticmethod
    def base64tostring(s): return _b64.b64decode(s).decode()
    @staticmethod
    def binary(s): return str(s).encode()
    @staticmethod
    def uricomponent(s): return urllib.parse.quote(str(s), safe="")
    @staticmethod
    def uricomponenttostring(s): return urllib.parse.unquote(str(s))
    @staticmethod
    def datauri(s): return "data:text/plain;charset=utf-8;base64," + _b64.b64encode(str(s).encode()).decode()
    @staticmethod
    def datauritostring(s): return _b64.b64decode(str(s).split(",", 1)[1]).decode()
    @staticmethod
    def guid(fmt="D"):
        g = uuid.uuid4()
        return {"N": g.hex, "D": str(g), "B": "{" + str(g) + "}", "P": "(" + str(g) + ")", "X": "{0x%s,0x%s,0x%s,{%s}}" % (g.hex[:8], g.hex[8:12], g.hex[12:16], ",".join("0x" + g.hex[i:i + 2] for i in range(16, 32, 2)))}.get(str(fmt).upper(), str(g))
    @staticmethod
    def min_(*a): return min(a[0]) if len(a) == 1 and isinstance(a[0], list) else min(a)
    @staticmethod
    def max_(*a): return max(a[0]) if len(a) == 1 and isinstance(a[0], list) else max(a)
    @staticmethod
    def range_(start, count): return list(range(start, start + count))

    @staticmethod
    def utcnow(fmt=None):
        d = datetime.now(timezone.utc)
        return _net_format(d, fmt or "o")

    @staticmethod
    def formatdatetime(v, fmt=None): return _net_format(_parse_dt(v), fmt or "o")
    @staticmethod
    def adddays(v, n, fmt=None): return fx.formatdatetime(_parse_dt(v) + timedelta(days=n), fmt)
    @staticmethod
    def addhours(v, n, fmt=None): return fx.formatdatetime(_parse_dt(v) + timedelta(hours=n), fmt)
    @staticmethod
    def addminutes(v, n, fmt=None): return fx.formatdatetime(_parse_dt(v) + timedelta(minutes=n), fmt)
    @staticmethod
    def addseconds(v, n, fmt=None): return fx.formatdatetime(_parse_dt(v) + timedelta(seconds=n), fmt)
    @staticmethod
    def addtotime(v, n, unit, fmt=None): return fx.formatdatetime(_shift(_parse_dt(v), n, unit), fmt)
    @staticmethod
    def subtractfromtime(v, n, unit, fmt=None): return fx.formatdatetime(_shift(_parse_dt(v), -n, unit), fmt)
    @staticmethod
    def getfuturetime(n, unit, fmt=None): return fx.formatdatetime(_shift(datetime.now(timezone.utc), n, unit), fmt)
    @staticmethod
    def getpasttime(n, unit, fmt=None): return fx.formatdatetime(_shift(datetime.now(timezone.utc), -n, unit), fmt)
    @staticmethod
    def startofday(v, fmt=None): return fx.formatdatetime(_parse_dt(v).replace(hour=0, minute=0, second=0, microsecond=0), fmt)
    @staticmethod
    def startofhour(v, fmt=None): return fx.formatdatetime(_parse_dt(v).replace(minute=0, second=0, microsecond=0), fmt)
    @staticmethod
    def startofmonth(v, fmt=None): return fx.formatdatetime(_parse_dt(v).replace(day=1, hour=0, minute=0, second=0, microsecond=0), fmt)
    @staticmethod
    def dayofmonth(v): return _parse_dt(v).day
    @staticmethod
    def dayofweek(v): return (_parse_dt(v).weekday() + 1) % 7            # Sunday is 0
    @staticmethod
    def dayofyear(v): return _parse_dt(v).timetuple().tm_yday

    @staticmethod
    def converttimezone(v, source, dest, fmt=None):
        d = _parse_dt(v).replace(tzinfo=_zone(source))                  # the timestamp's wall time is read in the source zone
        return _net_format(d.astimezone(_zone(dest)).replace(tzinfo=None), fmt or "o")

    @staticmethod
    def convertfromutc(v, dest, fmt=None): return _net_format(_parse_dt(v).astimezone(_zone(dest)).replace(tzinfo=None), fmt or "o")

    @staticmethod
    def converttoutc(v, source, fmt=None):
        d = _parse_dt(v).replace(tzinfo=_zone(source))
        return _net_format(d.astimezone(timezone.utc), fmt or "o")


# ---------------------------------------------------------------------------------------------------------------------------------------------
# pipeline run state
# ---------------------------------------------------------------------------------------------------------------------------------------------

_JSON_TYPES = {"array", "object"}


class Ctx:
    """State of one pipeline run: parameters, variables, activity outputs and statuses."""

    def __init__(self, pipeline: str, params: dict, param_types: dict, variables: dict, *, log: Callable[[str], None] = print,
                 trigger: dict | None = None, factory: str = "", globals_: dict | None = None, parent: tuple | None = None):
        self.pipeline, self.log, self._trigger, self.factory = pipeline, log, trigger or {}, factory
        self.parent = parent                                  # (name, run id) of the pipeline whose ExecutePipeline started this one
        self.run_id = str(uuid.uuid4())
        self.params = {}
        declared = {k.lower(): k for k in param_types}
        types_ci = {k.lower(): t for k, t in param_types.items()}
        for k, v in params.items():
            k = declared.get(k.lower(), k)                       # ADF parameter names are not case-sensitive
            t = types_ci.get(k.lower(), "").lower()
            if t in _JSON_TYPES and isinstance(v, str):
                try:
                    v = _json.loads(v) if v.strip() else ([] if t == "array" else {})
                except ValueError:
                    if not dry_run():
                        raise
                    v = [] if t == "array" else {}               # a dry run's stand-in value is not JSON
            self.params[k] = v
        self.vars = dict(variables)
        self.outputs: dict[str, Any] = {}
        self.errors: dict[str, str] = {}
        self.statuses: dict[str, str] = {}
        self.return_value: dict = {}
        self._globals = globals_ or {}
        self.lock = threading.Lock()

    @staticmethod
    def _ci(d: dict, name):
        """The key of d that matches name, ignoring case (ADF names are not case-sensitive); name itself when none does."""
        if name in d:
            return name
        low = str(name).lower()
        return next((k for k in d if str(k).lower() == low), name)

    def param(self, name):
        name = self._ci(self.params, name)
        if name not in self.params:
            raise AdfExprError(f"pipeline {self.pipeline!r} has no parameter {name!r}")
        return self.params[name]

    def global_param(self, name):
        key = "ADF_GLOBAL_" + re.sub(r"\W", "_", name).upper()
        if name in self._globals:
            return self._globals[name]
        if key in os.environ:
            return os.environ[key]
        if dry_run():
            return DRY
        raise AdfExprError(f"global parameter {name!r}: set environment variable {key}")

    def var(self, name):
        name = self._ci(self.vars, name)
        if name not in self.vars:
            raise AdfExprError(f"pipeline {self.pipeline!r} has no variable {name!r}")
        return self.vars[name]

    def set_return(self, values: dict):
        """The pipeline's return value (SetVariable on pipelineReturnValue): what an ExecutePipeline of this pipeline sees as pipelineReturnValue."""
        with self.lock:
            self.return_value = dict(values)

    def set_var(self, name, value):
        with self.lock:
            self.vars[self._ci(self.vars, name)] = value

    def append_var(self, name, value):
        with self.lock:
            name = self._ci(self.vars, name)
            cur = self.vars.get(name)
            if not isinstance(cur, list):
                cur = [] if cur in (None, "") else [cur]
            self.vars[name] = cur + [value]

    def out(self, name):
        name = self._ci(self.outputs, name)
        if name not in self.outputs:
            if dry_run():
                return DRY
            raise AdfExprError(f"activity {name!r} has not produced output (did it run?)")
        return self.outputs[name]

    def err(self, name):
        return {"message": self.errors.get(name, ""), "errorCode": "ActivityFailed"}

    def status(self, name):
        return self.statuses.get(name, "")

    def status_code(self, name):
        """The HTTP status of a Web activity (templates read activity('x').statusCode)."""
        o = self.outputs.get(name)
        if isinstance(o, dict) and "statusCode" in o:
            return o["statusCode"]
        if dry_run():
            return DRY
        raise AdfExprError(f"activity {name!r} has no HTTP status code")

    def sys(self, name):
        # In a Fabric pipeline pipeline().DataFactory is the workspace id; in ADF it is the factory name
        factory = os.environ.get("FABRIC_WORKSPACE_ID") or os.environ.get("ADF_FACTORY_NAME") or self.factory
        return {"pipeline": self.pipeline, "runid": self.run_id, "datafactory": factory,
                "triggertype": self._trigger.get("type", "Manual"), "triggerid": "", "triggername": self._trigger.get("name", "Manual"),
                "triggertime": self._trigger.get("startTime") or _iso(datetime.now(timezone.utc)), "groupid": self.run_id,
                "triggeredbypipelinename": self.parent[0] if self.parent else None, "triggeredbypipelinerunid": self.parent[1] if self.parent else None}[name]

    def trigger(self, name):
        for k, v in self._trigger.items():
            if k.lower() == name.lower():
                return v
        if dry_run():
            return DRY
        raise AdfExprError(f"trigger property {name!r} is not available: the pipeline was not started by a trigger that provides it")


def _met(statuses: dict, deps) -> bool:
    for act, conds in deps:
        s = statuses.get(act)
        if not any(c == s or (c == "Completed" and s in ("Succeeded", "Failed")) for c in conds):
            return False
    return True


def _duration(policy: dict, key: str, default: int = 0) -> int:
    v = policy.get(key, default)
    return int(v) if isinstance(v, (int, float)) else default


def _scope_failure(entries, statuses) -> str | None:
    """ADF's rule for whether a scope failed (Microsoft's error-handling tutorial): look at every leaf activity (one nothing depends on); a leaf that was
    skipped is judged by its parent instead, and so on up; the scope succeeds only if every judged node succeeded. Returns the name of a failed activity that
    decides it, or None. So a lone Upon Failure handler makes the scope succeed (try-catch), while both an Upon Failure and an Upon Success branch make it fail
    when the activity fails (the skipped success leaf points back at the failed parent)."""
    parents = {name: [d for d, _ in deps] for name, deps, _, _ in entries}
    depended = {d for ps in parents.values() for d in ps}
    seen: set = set()

    def verdict(n):
        if n in seen:
            return None
        seen.add(n)
        st = statuses.get(n)
        if st == "Failed":
            return n
        if st == "Skipped":
            for p in parents.get(n, []):
                r = verdict(p)
                if r:
                    return r
        return None

    for name, _, _, _ in entries:
        if name not in depended:
            seen = set()
            r = verdict(name)
            if r:
                return r
    return None


def run_activities(ctx: Ctx, item, entries) -> None:
    """Run (name, depends_on, fn, policy) entries in order, honoring dependency conditions (Succeeded, Failed, Skipped, Completed) and retry policy, then
    decide whether the scope failed by ADF's leaf rule (see _scope_failure); the failure raised is that of the activity that decides it."""
    failures: dict[str, BaseException] = {}
    for name, deps, fn, policy in entries:
        if not _met(ctx.statuses, deps):
            ctx.statuses[name] = "Skipped"
            ctx.log(f"[{ctx.pipeline}] {name}: Skipped")
            continue
        retries, wait = _duration(policy, "retry"), _duration(policy, "retryIntervalInSeconds", 30)
        for attempt in range(retries + 1):
            try:
                ctx.log(f"[{ctx.pipeline}] {name}: start")
                res = fn(ctx, item)
                with ctx.lock:
                    ctx.outputs[name] = res if res is not None else Out()
                    ctx.statuses[name] = "Succeeded"
                break
            except BaseException as e:      # noqa: BLE001 - recorded, re-raised below if the leaf rule says the scope failed
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise
                if attempt < retries and not isinstance(e, AdfCaveat):
                    ctx.log(f"[{ctx.pipeline}] {name}: failed ({e}); retry {attempt + 1}/{retries}")
                    if not dry_run():
                        time.sleep(wait)
                    continue
                with ctx.lock:
                    ctx.statuses[name] = "Failed"
                    ctx.errors[name] = str(e)
                ctx.log(f"[{ctx.pipeline}] {name}: Failed: {e}")
                failures[name] = e
                break
    decided = _scope_failure(entries, ctx.statuses)
    if decided:
        raise failures[decided]


def as_list(v) -> list:
    if isinstance(v, Dry) or v is None:
        return []
    if isinstance(v, list):
        return v
    raise AdfExprError(f"ForEach items must be an array, got {type(v).__name__}")


def adf_foreach(ctx: Ctx, items, body: Callable, sequential: bool, batch: int | None) -> Out:
    items = as_list(items)
    errors: list[BaseException] = []
    if sequential or len(items) <= 1:
        for it in items:
            try:
                body(ctx, it)
            except BaseException as e:      # noqa: BLE001
                errors.append(e)
    else:
        with ThreadPoolExecutor(max_workers=max(1, min(batch or 20, 50))) as pool:
            futs = [pool.submit(body, ctx, it) for it in items]
            for f in futs:
                try:
                    f.result()
                except BaseException as e:  # noqa: BLE001
                    errors.append(e)
    if errors:
        raise errors[0]
    return Out()


def adf_until(ctx: Ctx, item, body: Callable, cond: Callable, timeout_s: int) -> Out:
    """Run body until cond is true, or fail at the timeout. ADF's Until does not stop when an inner activity fails (it evaluates the condition and goes on), so a
    failure of the body is logged and the loop continues, except a stub (ADF-CAVEAT), which must stop the run. A dry run runs the body once: its Lookup results are
    empty, so the condition could never be met."""
    start = time.monotonic()
    while True:
        try:
            body(ctx, item)
        except AdfCaveat:
            raise
        except Exception as e:                               # noqa: BLE001
            if dry_run():
                raise
            ctx.log(f"[{ctx.pipeline}] Until: an inner activity failed ({e}); the loop continues")
        if dry_run():
            ctx.log(f"[{ctx.pipeline}] Until: dry run, body ran once")
            return Out()
        if cond(ctx, item):
            return Out()
        if time.monotonic() - start > timeout_s:
            raise AdfFail("Until activity timed out before its expression became true")


def adf_wait(ctx: Ctx, seconds) -> Out:
    s = int(float(seconds))
    ctx.log(f"[{ctx.pipeline}] Wait {s}s" + (" (dry run, not sleeping)" if dry_run() else ""))
    if not dry_run():
        time.sleep(s)
    return Out()


def adf_fail(message, code) -> None:
    raise AdfFail(f"{code}: {message}" if code else str(message))


def adf_filter(items, pred: Callable) -> Out:
    allv = as_list(items)
    kept = [i for i in allv if pred(i)]
    return Out({"ItemsCount": len(allv), "FilteredItemsCount": len(kept), "Value": kept})


def _msi_token(resource: str) -> str:
    """Bearer token for a resource through azure-identity's DefaultAzureCredential: the managed identity when running on Azure, a service principal
    from AZURE_CLIENT_ID / AZURE_TENANT_ID / AZURE_CLIENT_SECRET, or the Azure CLI login locally. Nothing is read from ADF."""
    from azure.identity import DefaultAzureCredential
    res = str(resource).rstrip("/")
    return DefaultAzureCredential().get_token(res + "/.default").token


def adf_web(ctx: Ctx, name: str, url, method: str = "GET", headers: dict | None = None, body=None, auth: dict | None = None, timeout_s: float | None = None,
            async_off: bool = False) -> Out:
    method = (method or "GET").upper()
    ctx.log(f"[{ctx.pipeline}] {name}: {method} {url}" + (" (dry run, not sent)" if dry_run() else ""))
    if dry_run():
        return Out()
    import urllib.request
    data = None
    h = {str(k): fx.string(v) for k, v in (headers or {}).items()}
    if auth and auth.get("type") == "MSI":
        h["Authorization"] = "Bearer " + _msi_token(auth["resource"])
    if body is not None and method != "GET":
        data = (_json.dumps(body) if not isinstance(body, (str, bytes)) else body)
        data = data.encode() if isinstance(data, str) else data
        h.setdefault("Content-Type", "application/json")
    timeout = timeout_s or float(os.environ.get("ADF_WEB_TIMEOUT", "60"))          # ADF: 1 minute unless httpRequestTimeout says otherwise (at most 10)
    req = urllib.request.Request(str(url), data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        text = r.read().decode("utf-8", "replace")
        hdrs = dict(r.headers.items())
        status = getattr(r, "status", 200)
    if status == 202 and not async_off and hdrs.get("Location"):
        # the asynchronous request-reply pattern: keep GETting the Location header until the endpoint stops answering 202 (ADF waits up to 7 days)
        loc, give_up = hdrs["Location"], time.monotonic() + float(os.environ.get("ADF_WEB_ASYNC_MAX", str(7 * 86400)))
        while time.monotonic() < give_up:
            time.sleep(float(os.environ.get("ADF_WEB_POLL", "5")))
            with urllib.request.urlopen(urllib.request.Request(loc, headers=h, method="GET"), timeout=timeout) as r2:
                text = r2.read().decode("utf-8", "replace")
                hdrs = dict(r2.headers.items())
                status = getattr(r2, "status", 200)
            if status != 202:
                break
            loc = hdrs.get("Location", loc)
        else:
            raise AdfFail(f"{name}: the asynchronous request did not complete in time")
    try:
        parsed = _json.loads(text)
    except ValueError:
        parsed = None
    if parsed is not None and not isinstance(parsed, dict):
        raise AdfFail(f"{name}: the endpoint must return a JSON object (the Web activity page lists arrays, plain values and non-JSON bodies as unsupported responses)")
    if parsed is None and text.strip():
        raise AdfFail(f"{name}: the endpoint must return a JSON object, not {text[:60]!r}")
    out = Out(parsed) if isinstance(parsed, dict) else Out()
    out["ADFWebActivityResponseHeaders"] = hdrs
    out.setdefault("statusCode", status)
    return out


def _conn_url(env: str) -> str | None:
    return os.environ.get(env)


LOOKUP_ROWS, LOOKUP_BYTES = 5000, 4 * 1024 * 1024


def _lookup_limits(name: str, rows: list) -> list:
    """The Lookup activity returns at most 5000 rows (the rest are dropped without an error) and fails when its output exceeds 4 MB."""
    if len(rows) > LOOKUP_ROWS:
        rows = rows[:LOOKUP_ROWS]
    if len(_json.dumps(rows, default=str)) > LOOKUP_BYTES:
        raise AdfFail(f"{name}: the Lookup output exceeds 4 MB")
    return rows


def adf_sql(ctx: Ctx, name: str, conn_env: str, sql: str, params: dict | None = None, fetch: str = "none", lookup: bool = False) -> Out:
    """Run SQL over a SQLAlchemy URL from the environment. fetch: none | first | all. lookup applies the Lookup activity's row and size limits."""
    ctx.log(f"[{ctx.pipeline}] {name}: SQL on {conn_env}: {sql}" + (" (dry run, not executed)" if dry_run() else ""))
    if dry_run():
        return Out()
    url = _conn_url(conn_env)
    if not url:
        raise AdfExprError(f"set {conn_env} to a SQLAlchemy URL for this linked service")
    from sqlalchemy import create_engine, text
    eng = create_engine(url)
    with eng.begin() as con:
        res = con.execute(text(sql), params or {})
        if fetch == "none" or not res.returns_rows:
            return Out({"rowsAffected": res.rowcount})
        rows = [dict(r._mapping) for r in res]
    if lookup:
        rows = _lookup_limits(name, rows)
    return Out({"firstRow": rows[0] if rows else None, "count": len(rows)}) if fetch == "first" else Out({"value": rows, "count": len(rows)})


def window_trigger(name: str, context) -> dict:
    """Trigger properties for a run of a tumbling-window pipeline, from the run's partition."""
    w = context.partition_time_window
    return {"type": "TumblingWindowTrigger", "name": name, "windowStartTime": _iso(w.start.astimezone(timezone.utc)), "windowEndTime": _iso(w.end.astimezone(timezone.utc)),
            "scheduledTime": _iso(w.end.astimezone(timezone.utc)), "startTime": _iso(w.end.astimezone(timezone.utc))}


def adf_databricks_notebook(ctx: Ctx, name: str, notebook_path, base_parameters: dict | None = None, existing_cluster_id: str = "",
                            new_cluster: dict | None = None) -> Out:
    """Run a notebook as a one-off Databricks run (jobs.submit) and wait for it. Auth is the SDK's own (DATABRICKS_HOST and DATABRICKS_TOKEN,
    or any other unified-auth setting in the environment): credentials are never generated into code."""
    params = {str(k): fx.string(v) for k, v in (base_parameters or {}).items()}
    ctx.log(f"[{ctx.pipeline}] {name}: Databricks notebook {notebook_path} params={params}" + (" (dry run, not submitted)" if dry_run() else ""))
    if dry_run():
        return Out()
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service import compute, jobs
    kw: dict = {}
    if existing_cluster_id:
        kw["existing_cluster_id"] = existing_cluster_id
    elif new_cluster:
        spec = dict(new_cluster)
        if isinstance(spec.get("autoscale"), dict):
            spec["autoscale"] = compute.AutoScale(**spec["autoscale"])
        kw["new_cluster"] = compute.ClusterSpec(**spec)
    else:
        raise AdfExprError(f"{name}: the activity names no cluster")
    w = WorkspaceClient()
    task = jobs.SubmitTask(task_key=re.sub(r"\W", "_", name)[:100] or "notebook",
                           notebook_task=jobs.NotebookTask(notebook_path=str(notebook_path), base_parameters=params), **kw)
    run = w.jobs.submit(run_name=f"{ctx.pipeline}/{name}", tasks=[task]).result()
    result = None
    if run.tasks:
        out = w.jobs.get_run_output(run.tasks[0].run_id)
        result = out.notebook_output.result if out.notebook_output else None
    return Out({"runPageUrl": run.run_page_url, "runOutput": result})


# ---------------------------------------------------------------------------------------------------------------------------------------------
# Copy activity: the common pairs. Stores are reached through fsspec (abfs:// for blob and ADLS Gen2, s3://, local paths); the storage options of
# a linked service come from the environment variable ADF_FS_<LINKED_SERVICE> as a JSON object, so no credential is ever generated into code.
# ---------------------------------------------------------------------------------------------------------------------------------------------

def _fs(env: str, url: str):
    import fsspec
    opts: dict = {}
    if env:
        raw = os.environ.get(env)
        if not raw:
            raise AdfExprError(f"set {env} to a JSON object of fsspec storage options for this linked service (for example account_name and account_key)")
        opts = _json.loads(raw)
    return fsspec.core.url_to_fs(str(url), **opts)


def _copy_out(files: int, nbytes: int, rows: int | None = None) -> Out:
    o = Out({"filesRead": files, "filesWritten": files, "dataRead": nbytes, "dataWritten": nbytes})
    if rows is not None:
        o["rowsCopied"] = rows
        o["rowsRead"] = rows
    return o


def _wild(pattern: str):
    """ADF's wildcard: * matches zero or more characters, ? zero or one, ^ escapes the next character; the whole name must match."""
    out, i = [], 0
    pat = str(pattern)
    while i < len(pat):
        c = pat[i]
        if c == "^" and i + 1 < len(pat):
            out.append(re.escape(pat[i + 1]))
            i += 2
            continue
        out.append(".*" if c == "*" else ".?" if c == "?" else re.escape(c))
        i += 1
    return re.compile("".join(out), re.S)


def _file_mtime(fs, path: str):
    """A file's last-modified time as an aware UTC datetime, or None when the store does not report one."""
    info = fs.info(path)
    for k in ("LastModified", "last_modified", "mtime", "modified", "created"):
        v = info.get(k)
        if v is None:
            continue
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v, timezone.utc)
        if isinstance(v, datetime):
            return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        try:
            return _parse_dt(v)
        except AdfExprError:
            continue
    return None


def _find_files(sfs, spath: str, recursive: bool, wildcard: str, wildcard_folder: str = "", filters: dict | None = None) -> tuple[list[str], str]:
    """The files a source folder or file names: (paths, the directory their relative names are taken from).
    `wildcard_folder` is ADF's wildcardFolderPath: a pattern for folders under spath, matched segment by segment (so * never crosses a /). A file qualifies when
    its folder is one of the matching folders, or, when recursive, below one.
    `filters` (connector page): file_list, a text file of relative paths (one per line) that replaces the search; prefix, keys under spath starting with it;
    modified_start (inclusive) and modified_end (exclusive), UTC, on the file's last-modified time."""
    import posixpath
    filters = filters or {}
    if sfs.isfile(spath):
        return [spath], posixpath.dirname(spath)
    base = spath.rstrip("/")
    if filters.get("file_list"):
        with sfs.open(str(filters["file_list"]), "rb") as fh:
            names = [ln.strip() for ln in fh.read().decode("utf-8").splitlines() if ln.strip()]
        found = [posixpath.join(base, n) for n in names]
        missing = [f for f in found if not sfs.exists(f)]
        if missing:
            raise AdfExprError(f"the file list names files that do not exist: {missing[:3]}")
        out = found
    else:
        name_re = _wild(wildcard or "*")
        prefix = str(filters.get("prefix") or "")
        out = []
        for f in sfs.find(spath):
            rel = posixpath.relpath(f, base)
            if prefix and not rel.startswith(prefix):
                continue
            if not name_re.fullmatch(posixpath.basename(rel)):
                continue
            dirs = [d for d in posixpath.dirname(rel).split("/") if d and d != "."]
            if wildcard_folder:
                segs = [x for x in str(wildcard_folder).strip("/").split("/") if x]
                ok = len(dirs) >= len(segs) and all(_wild(sg).fullmatch(dirs[i]) for i, sg in enumerate(segs)) and (recursive or len(dirs) == len(segs))
            else:
                ok = recursive or not dirs
            if ok:
                out.append(f)
    lo = _parse_dt(filters["modified_start"]) if filters.get("modified_start") else None
    hi = _parse_dt(filters["modified_end"]) if filters.get("modified_end") else None
    if lo or hi:
        kept = []
        for f in out:
            m = _file_mtime(sfs, f)
            if m is not None and (lo is None or m >= lo) and (hi is None or m < hi):
                kept.append(f)
        out = kept
    return sorted(out), base


def _partition_columns(path: str, root: str) -> dict:
    """The key=value folders between root and a file, as extra columns (enablePartitionDiscovery)."""
    import posixpath
    rel = posixpath.dirname(posixpath.relpath(path, root)) if root else posixpath.dirname(path)
    return dict(seg.split("=", 1) for seg in rel.split("/") if "=" in seg)


def adf_copy_files(ctx: Ctx, name: str, src_url, src_env: str, dst_url, dst_env: str, dst_file: str = "", recursive: bool = True,
                   wildcard: str = "", flatten: bool = False, wildcard_folder: str = "", filters: dict | None = None, merge: bool = False,
                   delete_source: bool = False) -> Out:
    """Byte copy of the files a source folder or file names into a sink folder (same format on both sides)."""
    ctx.log(f"[{ctx.pipeline}] {name}: copy files {src_url} -> {dst_url}" + (f"/{dst_file}" if dst_file else "") + (" (dry run, not copied)" if dry_run() else ""))
    if dry_run():
        return Out()
    import posixpath
    import shutil
    sfs, spath = _fs(src_env, src_url)
    dfs, dpath = _fs(dst_env, dst_url)
    files, base = _find_files(sfs, spath, recursive, wildcard, wildcard_folder, filters)
    nbytes, used = 0, set()
    if merge and files:
        # copyBehavior MergeFiles: the contents of all the files, in name order, in one file (named by the sink, else autogenerated)
        ext = posixpath.splitext(files[0])[1]
        target = posixpath.join(dpath, dst_file or f"data_{uuid.uuid4()}{ext}")
        dfs.makedirs(posixpath.dirname(target), exist_ok=True)
        with dfs.open(target, "wb") as w:
            for f in files:
                with sfs.open(f, "rb") as r:
                    shutil.copyfileobj(r, w)
        nbytes = dfs.size(target) or 0
        if delete_source:
            for f in files:
                sfs.rm(f)
        return _copy_out(len(files), nbytes)
    for f in files:
        if dst_file and len(files) == 1:
            target = posixpath.join(dpath, dst_file)
        elif flatten:
            stem, ext = posixpath.splitext(posixpath.basename(f))
            name, k = stem + ext, 1
            while name in used:                              # ADF autogenerates names when flattening; keep ours readable and never overwrite
                name, k = f"{stem}_{k}{ext}", k + 1
            used.add(name)
            target = posixpath.join(dpath, name)
        else:
            target = posixpath.join(dpath, posixpath.relpath(f, base)) if base else posixpath.join(dpath, f)
        dfs.makedirs(posixpath.dirname(target), exist_ok=True)
        with sfs.open(f, "rb") as r, dfs.open(target, "wb") as w:
            shutil.copyfileobj(r, w)
        nbytes += dfs.size(target) or 0
        if delete_source:
            sfs.rm(f)                                    # per file, after it was copied: a failure leaves the rest where they were, as documented
    return _copy_out(len(files), nbytes)


def _jpath(obj, path: str):
    """A simple JSONPath: $.a.b, $['a'], $.['a.b'], $.items[0]. None when any step is missing."""
    p = str(path).strip()
    if p.startswith("$"):
        p = p[1:]
    cur = obj
    for m in re.finditer(r"\.?\[['\"]([^'\"]+)['\"]\]|\[(\d+)\]|\.([A-Za-z_@][\w@-]*)", p):
        key = m.group(1) or m.group(3)
        try:
            cur = cur[key] if key is not None else cur[int(m.group(2))]
        except (KeyError, IndexError, TypeError):
            return None
    return cur


def hierarchical_rows(doc, mappings, collection: str = "") -> list:
    """Rows from a hierarchical document by the Copy activity's path mappings. A path starting with $ is read from the document root, any other path from the
    current element of the collection reference's array (one row per element, the root values repeated); with no collection the document is one row. An empty
    collection array skips the record. mappings: [(source path, sink name)]."""
    def val(path, item):
        p = str(path).strip()
        return _jpath(doc if (p.startswith("$") or item is None) else item, p)

    if collection:
        items = _jpath(doc, collection)
        if not isinstance(items, list):
            return []
        return [{sink: val(path, it) for path, sink in mappings} for it in items]
    return [{sink: val(path, None) for path, sink in mappings}]


def _rule_value(rule, doc, resp_headers):
    """A pagination rule's value: a response header (Headers.x) or a JSONPath into the response body ($...). None when it is missing or null."""
    r = str(rule).strip()
    m = re.fullmatch(r"Headers(?:\.([\w-]+)|\['([^']+)'\])", r)
    if m:
        want = (m.group(1) or m.group(2)).lower()
        return next((v for k, v in resp_headers.items() if k.lower() == want), None)
    return _jpath(doc, r) if r.startswith("$") else None


def _end_reached(conds: dict, doc, resp_headers) -> bool:
    """EndCondition:<$.path or headers.name> with Empty, NonExist, Exist or Const:<value> (REST connector page)."""
    for key, cond in conds.items():
        if key.lower().startswith("headers."):
            want = key.split(".", 1)[1].lower()
            val = next((v for k, v in resp_headers.items() if k.lower() == want), None)
        else:
            val = _jpath(doc, key)
        c = str(cond)
        if (c == "Empty" and (val is None or val == "" or val == [] or val == {})) or (c == "NonExist" and val is None) or (c == "Exist" and val is not None) \
                or (c.startswith("Const:") and val is not None and str(val).lower() == c[6:].lower()):
            return True
    return False


def _set_query(url: str, name: str, value) -> str:
    parts = urllib.parse.urlsplit(url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if k != name] + [(name, str(value))]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q)))


def rest_pages(ctx: Ctx, fetch, url: str, headers: dict, body, rules: dict | None, interval_s: float = 0, max_pages: int = 100000) -> list:
    """The response bodies of a REST copy, following the documented pagination rules. fetch(url, headers, body) -> (status, response headers, parsed body).
    Rules are applied from the second request: AbsoluteUrl, QueryParameters.x and Headers.x take their value from the previous response (a $ JSONPath or
    Headers.name); a rule whose value is missing or null ends the loop, as does HTTP 204. A "RANGE:start:end:step" value on AbsoluteUrl.{v}, QueryParameters.{v}
    or Headers.{v} instead sends one request per value with {v} replaced in the url and header values (end inclusive; an empty end runs until an end
    condition). EndCondition:*, MaxRequestNumber and, when no rule is given, RFC 5988 Link headers are as documented."""
    rules = dict(rules or {})
    ends = {k[len("EndCondition:"):]: v for k, v in rules.items() if k.startswith("EndCondition:")}
    max_req = int(rules["MaxRequestNumber"]) if str(rules.get("MaxRequestNumber") or "").strip() else None
    support_link = str(rules.get("SupportRFC5988", "true" if not [k for k in rules if k not in ("MaxRequestNumber", "SupportRFC5988") and not k.startswith("EndCondition:")] else "false")).lower() != "false"
    ranges = {}
    chain = {}
    for k, v in rules.items():
        m = re.fullmatch(r"(?:AbsoluteUrl|QueryParameters|Headers)\.\{(\w+)\}", k)
        if m and str(v).startswith("RANGE:"):
            _, lo, hi, step = (str(v).split(":") + ["", "", ""])[:4]
            ranges[m.group(1)] = (int(lo), int(hi) if hi.strip() else None, int(step or 1))
        elif k not in ("MaxRequestNumber", "SupportRFC5988") and not k.startswith("EndCondition:"):
            chain[k] = v
    pages, cur_url, cur_headers = [], str(url), dict(headers)
    value = None
    if ranges:
        var, (lo, hi, step) = next(iter(ranges.items()))
        value = lo
    n = 0
    while n < max_pages:
        if ranges:
            if hi is not None and value > hi:
                break
            u = cur_url.replace("{" + var + "}", str(value))
            h = {k: str(v).replace("{" + var + "}", str(value)) for k, v in cur_headers.items()}
            value += step
        else:
            u, h = cur_url, cur_headers
        status, rh, doc = fetch(u, h, body if n == 0 else None)
        n += 1
        if status == 204:
            break
        pages.append(doc)
        if _end_reached(ends, doc, rh) or (max_req and n >= max_req):
            break
        if not ranges:
            nxt_url, nxt_headers, stop = u, dict(h), False
            for k, rule in chain.items():
                val = _rule_value(rule, doc, rh)
                if val is None:
                    stop = True
                    break
                if k == "AbsoluteUrl":
                    nxt_url = urllib.parse.urljoin(u, str(val))
                elif re.match(r"QueryParameters(\.|\[)", k):
                    nxt_url = _set_query(nxt_url, re.fullmatch(r"QueryParameters(?:\.([\w-]+)|\['([^']+)'\])", k).group(1) or re.fullmatch(r"QueryParameters(?:\.([\w-]+)|\['([^']+)'\])", k).group(2), val)
                elif re.match(r"Headers(\.|\[)", k):
                    mm = re.fullmatch(r"Headers(?:\.([\w-]+)|\['([^']+)'\])", k)
                    nxt_headers[mm.group(1) or mm.group(2)] = str(val)
                else:
                    raise AdfExprError(f"pagination rule {k!r} is not supported")
            if not chain and support_link:
                link = next((v for k, v in rh.items() if k.lower() == "link"), "")
                m = re.search(r'<([^>]+)>\s*;\s*rel="?next"?', link)
                if not m:
                    break
                nxt_url = urllib.parse.urljoin(u, m.group(1))
            elif stop or not chain:
                break
            cur_url, cur_headers = nxt_url, nxt_headers
        if interval_s:
            time.sleep(interval_s)
    return pages


def adf_copy_rest(ctx: Ctx, name: str, url, method: str, headers: dict | None, body, dst_url, dst_env: str, dst_file: str,
                  pagination: dict | None = None, collection: str = "", interval_s: float = 0, dst_fmt: str = "Json", dst_opts: dict | None = None,
                  path_mappings: list | None = None, timeout_s: float = 100) -> Out:
    """Call a REST endpoint and write the response to one file. With no pagination, collection reference or mapping, the body is written as received; otherwise
    the rows (the items at `collection`, else each page, or the path mappings' columns) are written one JSON object per line or as delimited text, following the
    documented pagination rules (see rest_pages)."""
    ctx.log(f"[{ctx.pipeline}] {name}: REST {method} {url} -> {dst_url}/{dst_file}" + (" (dry run, not called)" if dry_run() else ""))
    if dry_run():
        return Out()
    import posixpath
    import urllib.request
    base_headers = {str(k): fx.string(v) for k, v in (headers or {}).items()}

    def fetch(u, h, b=None):
        data = None
        if b is not None and str(method).upper() != "GET":
            data = (b if isinstance(b, (str, bytes)) else _json.dumps(b))
            data = data.encode() if isinstance(data, str) else data
        req = urllib.request.Request(str(u), data=data, headers=h, method=str(method).upper())
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            raw, status, rh = r.read(), getattr(r, "status", 200), dict(r.headers.items())
        try:
            doc = _json.loads(raw) if raw.strip() else None
        except ValueError:
            doc = None
        return status, rh, (doc, raw)

    nrows = None
    if not pagination and not collection and not path_mappings and dst_fmt == "Json":
        payload = fetch(url, base_headers, body)[2][1]
    else:
        pages = rest_pages(ctx, lambda u, h, b: (lambda st, rh, d: (st, rh, d[0]))(*fetch(u, h, b)), str(url), base_headers, body, pagination, interval_s)
        rows = []
        for page in pages:
            if path_mappings:
                rows.extend(hierarchical_rows(page, path_mappings, collection))
            else:
                items = _jpath(page, collection) if collection else [page]
                rows.extend(items if isinstance(items, list) else ([] if items is None else [items]))
        nrows = len(rows)
        if dst_fmt == "DelimitedText":
            import pandas as pd
            df = pd.DataFrame(rows, columns=[c for _, c in path_mappings]) if path_mappings else pd.json_normalize(rows)
            dfs0, dpath0 = _fs(dst_env, dst_url)
            n = _write_frame(df, dfs0, posixpath.join(dpath0, dst_file), "DelimitedText", dst_opts or {})
            return _copy_out(1, n, nrows)
        payload = "\n".join(_json.dumps(r) for r in rows).encode()
    dfs, dpath = _fs(dst_env, dst_url)
    target = posixpath.join(dpath, dst_file)
    dfs.makedirs(posixpath.dirname(target), exist_ok=True)
    with dfs.open(target, "wb") as w:
        w.write(payload)
    return _copy_out(1, len(payload), nrows)


def _split_table(table: str) -> tuple[str | None, str]:
    parts = [p.strip('[]"` ') for p in str(table).split(".")]
    return (parts[-2] if len(parts) > 1 else None), parts[-1]


def adf_copy_csv_to_sql(ctx: Ctx, name: str, src_url, src_env: str, conn_env: str, table, *, delimiter: str = ",", quote: str = '"',
                        escape: str = "\\", encoding: str = "utf-8", recursive: bool = True, wildcard: str = "", pre_script: str = "",
                        mapping: dict | None = None, batch: int = 1000, wildcard_folder: str = "", header: bool = True, null_value: str = "",
                        keys: list | None = None, auto_create: bool = False, filters: dict | None = None, partition_root: str | None = None) -> Out:
    """Insert (or upsert) the rows of delimited text files into a table. Values go in as text and the null value as NULL; the database converts."""
    ctx.log(f"[{ctx.pipeline}] {name}: insert {src_url} into {table} on {conn_env}" + (" (dry run, not executed)" if dry_run() else ""))
    if dry_run():
        return Out()
    import csv
    url = os.environ.get(conn_env)
    if not url:
        raise AdfExprError(f"set {conn_env} to a SQLAlchemy URL for this linked service")
    from sqlalchemy import create_engine, text
    sfs, spath = _fs(src_env, src_url)
    files, base = _find_files(sfs, spath, recursive, wildcard, wildcard_folder, filters)
    eng, rows_total, nbytes = create_engine(url), 0, 0
    with eng.begin() as con:
        if pre_script:
            con.execute(text(pre_script))
        for f in files:
            with sfs.open(f, "rb") as raw:
                content = raw.read()
            nbytes += len(content)
            lines = content.decode(encoding).splitlines()
            if header:
                reader = csv.DictReader(lines, delimiter=delimiter, quotechar=quote or None)
            else:                                            # no header row: columns are Prop_0, Prop_1, ... (a mapping by ordinal names them)
                reader = ({f"Prop_{i}": v for i, v in enumerate(r)} for r in csv.reader(lines, delimiter=delimiter, quotechar=quote or None))
            part = _partition_columns(f, partition_root or base) if partition_root is not None else {}
            recs = [{(mapping or {}).get(k, k): (v if v != null_value else None) for k, v in {**row, **part}.items() if (not mapping or k in mapping)} for row in reader]
            rows_total += _insert_records(con, table, recs, batch, keys=keys, auto_create=auto_create)
    return _copy_out(len(files), nbytes, rows_total)


def adf_copy_sql_to_csv(ctx: Ctx, name: str, conn_env: str, query: str, dst_url, dst_env: str, dst_file: str, *, delimiter: str = ",",
                        quote: str = '"', header: bool = True, encoding: str = "utf-8", row_delimiter: str = "\r\n", null_value: str = "", mapping=None,
                        params: dict | None = None, isolation: str = "") -> Out:
    """Run a query and write the rows to one delimited text file."""
    ctx.log(f"[{ctx.pipeline}] {name}: {query} on {conn_env} -> {dst_url}/{dst_file}" + (" (dry run, not executed)" if dry_run() else ""))
    if dry_run():
        return Out()
    import csv
    import io
    import posixpath
    url = os.environ.get(conn_env)
    if not url:
        raise AdfExprError(f"set {conn_env} to a SQLAlchemy URL for this linked service")
    from sqlalchemy import create_engine, text
    with _read_conn(create_engine(url), isolation) as con:
        res = con.execute(text(query), params or {})
        cols, rows = list(res.keys()), res.fetchall()
    buf = io.StringIO()
    if mapping:
        keep = [i for i, c in enumerate(cols) if c in mapping]
        cols, rows = [mapping[cols[i]] for i in keep], [[r[i] for i in keep] for r in rows]
    w = csv.writer(buf, delimiter=delimiter, quotechar=quote or '"', lineterminator=row_delimiter or "\r\n")
    if header:
        w.writerow(cols)
    w.writerows([[null_value if v is None else v for v in r] for r in rows])
    payload = buf.getvalue().encode(encoding)
    dfs, dpath = _fs(dst_env, dst_url)
    target = posixpath.join(dpath, dst_file)
    dfs.makedirs(posixpath.dirname(target), exist_ok=True)
    with dfs.open(target, "wb") as f:
        f.write(payload)
    return _copy_out(1, len(payload), len(rows))


# ---------------------------------------------------------------------------------------------------------------------------------------------
# Storage activities: Delete, GetMetadata, Validation, and Lookup over a file
# ---------------------------------------------------------------------------------------------------------------------------------------------

def adf_delete(ctx: Ctx, name: str, url, env: str, recursive: bool = False, wildcard: str = "", filters: dict | None = None) -> Out:
    """Delete the file a location names, or the files in a folder that match (the folder itself too when recursive and no pattern is given)."""
    ctx.log(f"[{ctx.pipeline}] {name}: delete {url}" + (f" matching {wildcard}" if wildcard else "") + (" (dry run, not deleted)" if dry_run() else ""))
    if dry_run():
        return Out()
    fs, path = _fs(env, url)
    if not fs.exists(path):
        raise AdfExprError(f"{name}: path {path!r} does not exist")
    if fs.isfile(path):
        fs.rm(path)
        return Out()
    if wildcard or not recursive or filters:
        files, _ = _find_files(fs, path, recursive, wildcard, "", filters)
        for f in files:
            fs.rm(f)
    else:
        fs.rm(path, recursive=True)
    return Out()


def _iso_mtime(info: dict):
    import datetime as _dt
    for k in ("LastModified", "last_modified", "mtime", "modified", "created"):
        v = info.get(k)
        if v is None:
            continue
        if isinstance(v, (int, float)):
            return _iso(_dt.datetime.fromtimestamp(v, _dt.timezone.utc))
        if hasattr(v, "astimezone"):
            return _iso(v)
        return str(v)
    return None


SUPPORTED_METADATA = {"exists", "itemname", "itemtype", "size", "lastmodified", "childitems"}


def adf_get_metadata(ctx: Ctx, name: str, url, env: str, fields: list) -> Out:
    """The metadata fields ADF's GetMetadata offers for a file or folder: exists, itemName, itemType, size, lastModified, childItems."""
    ctx.log(f"[{ctx.pipeline}] {name}: metadata {sorted(fields)} of {url}" + (" (dry run, not read)" if dry_run() else ""))
    if dry_run():
        return Out()
    import posixpath
    fs, path = _fs(env, url)
    want = {str(f).lower() for f in fields}
    out = Out()
    exists = fs.exists(path)
    if "exists" in want:
        out["exists"] = exists
    if not exists:
        if "exists" in want:
            return out                       # listing exists means a missing path is reported, not an error
        raise AdfExprError(f"{name}: path {path!r} does not exist")
    info = fs.info(path)
    is_dir = info.get("type") == "directory"
    if "itemname" in want:
        out["itemName"] = posixpath.basename(path.rstrip("/"))
    if "itemtype" in want:
        out["itemType"] = "Folder" if is_dir else "File"
    if "size" in want:
        if is_dir:
            raise AdfExprError(f"{name}: size is not available for a folder")
        out["size"] = info.get("size")
    if "lastmodified" in want:
        out["lastModified"] = _iso_mtime(info)
    if "childitems" in want:
        if not is_dir:
            raise AdfExprError(f"{name}: childItems needs a folder")
        out["childItems"] = [{"name": posixpath.basename(e["name"].rstrip("/")), "type": "Folder" if e.get("type") == "directory" else "File"}
                             for e in sorted(fs.ls(path, detail=True), key=lambda e: e["name"])]
    return out


def adf_validate(ctx: Ctx, name: str, url, env: str, timeout_s: int, sleep_s: int, min_size=None, child_items=None) -> Out:
    """ADF's Validation: wait until the path exists. A file must also be at least min_size bytes. For a folder, child_items True waits until it has items, False until
    it is empty, and None only until it exists."""
    ctx.log(f"[{ctx.pipeline}] {name}: wait for {url} (timeout {timeout_s}s, every {sleep_s}s)" + (" (dry run, not waiting)" if dry_run() else ""))
    if dry_run():
        return Out()
    fs, path = _fs(env, url)
    start = time.monotonic()
    while True:
        if fs.exists(path):
            info = fs.info(path)
            if info.get("type") == "directory":
                ok = True if child_items is None else (bool(fs.ls(path)) == bool(child_items))
            else:
                ok = min_size is None or (info.get("size") or 0) >= int(min_size)
            if ok:
                return Out()
        if time.monotonic() - start >= timeout_s:
            raise AdfFail(f"{name}: {path} was not available within {timeout_s} seconds")
        time.sleep(max(0, sleep_s))


def adf_lookup_file(ctx: Ctx, name: str, url, env: str, fmt: str, first_row_only: bool = True, recursive: bool = False, wildcard: str = "",
                    wildcard_folder: str = "", delimiter: str = ",", quote: str = '"', header: bool = False, encoding: str = "utf-8", filters: dict | None = None) -> Out:
    """Rows of delimited text or JSON files, as a Lookup returns them: firstRow, or value and count."""
    ctx.log(f"[{ctx.pipeline}] {name}: lookup {fmt} {url}" + (" (dry run, not read)" if dry_run() else ""))
    if dry_run():
        return Out()
    import csv
    fs, path = _fs(env, url)
    files, _ = _find_files(fs, path, recursive, wildcard, wildcard_folder, filters)
    rows: list = []
    for f in files:
        with fs.open(f, "rb") as r:
            text = r.read().decode(encoding)
        if fmt == "DelimitedText":
            if header:
                rows.extend(dict(x) for x in csv.DictReader(text.splitlines(), delimiter=delimiter, quotechar=quote or None))
            else:
                rows.extend({f"Prop_{i}": v for i, v in enumerate(x)} for x in csv.reader(text.splitlines(), delimiter=delimiter, quotechar=quote or None))
        else:
            t = text.strip()
            rows.extend(_json.loads(t) if t.startswith("[") else [_json.loads(line) for line in t.splitlines() if line.strip()])
    rows = _lookup_limits(name, rows)
    if first_row_only:
        if not rows:
            raise AdfExprError(f"{name}: the source returned no rows")
        return Out({"firstRow": rows[0]})
    return Out({"count": len(rows), "value": rows})


def adf_function(ctx: Ctx, name: str, app_url: str, function, method: str, headers: dict | None, body, key_env: str, url_env: str = "") -> Out:
    """An Azure Function activity: a call to <function app url>/api/<function name>. The function key is read from the environment variable named
    by key_env (it is a secret in the linked service and is never copied); without it the call goes out without a key."""
    h = dict(headers or {})
    if not app_url and url_env:
        app_url = os.environ.get(url_env, "")
        if not app_url and not dry_run():
            raise AdfExprError(f"set {url_env} to the function app url")
    key = os.environ.get(key_env) if key_env else None
    if key:
        h["x-functions-key"] = key
    url = str(app_url).rstrip("/") + "/api/" + str(function).lstrip("/")
    return adf_web(ctx, name, url, method, h, body, timeout_s=230)          # Azure Functions time out after 230 seconds whatever functionTimeout says


def adf_copy_sql_to_json(ctx: Ctx, name: str, conn_env: str, query: str, dst_url, dst_env: str, dst_file: str, *, array: bool = False,
                         encoding: str = "utf-8", mapping=None, params: dict | None = None, isolation: str = "") -> Out:
    """Run a query and write the rows to one JSON file: one object per line (ADF's setOfObjects), or a JSON array."""
    ctx.log(f"[{ctx.pipeline}] {name}: {query} on {conn_env} -> {dst_url}/{dst_file}" + (" (dry run, not executed)" if dry_run() else ""))
    if dry_run():
        return Out()
    import posixpath
    url = os.environ.get(conn_env)
    if not url:
        raise AdfExprError(f"set {conn_env} to a SQLAlchemy URL for this linked service")
    from sqlalchemy import create_engine, text
    with _read_conn(create_engine(url), isolation) as con:
        res = con.execute(text(query), params or {})
        rows = [dict(r._mapping) for r in res]
    if mapping:
        rows = [{mapping[k]: v for k, v in r.items() if k in mapping} for r in rows]
    payload = (_json.dumps(rows, default=str) if array else "\n".join(_json.dumps(r, default=str) for r in rows)).encode(encoding)
    dfs, dpath = _fs(dst_env, dst_url)
    target = posixpath.join(dpath, dst_file)
    dfs.makedirs(posixpath.dirname(target), exist_ok=True)
    with dfs.open(target, "wb") as f:
        f.write(payload)
    return _copy_out(1, len(payload), len(rows))


_ADF_TYPES = {"int": "int_", "integer": "int_", "float": "float_", "double": "float_", "bool": "bool_", "boolean": "bool_"}


def adf_fabric_run(ctx: Ctx, name: str, item_type: str, item_id, workspace_id, params: dict | None = None, timeout_s: int = 1800, poll_s: int = 15) -> Out:
    """Run a Fabric notebook, pipeline or dataflow through the registry's FabricWorkspaceResource (the client behind fabric_workspace, which picks the right
    parameter shape per item type) and wait for it. Auth is azure-identity: AZURE_TENANT_ID / AZURE_CLIENT_ID / AZURE_CLIENT_SECRET when all are set, else
    DefaultAzureCredential. A run that does not end Completed fails the activity. The notebook's exit value is not available from the job API."""
    ctx.log(f"[{ctx.pipeline}] {name}: Fabric {item_type} {item_id} in workspace {workspace_id} params={params or {}}" + (" (dry run, not started)" if dry_run() else ""))
    if dry_run():
        return Out()
    from types import SimpleNamespace
    from dagster_community_components import FabricWorkspaceResource
    res = FabricWorkspaceResource(workspace_id=str(workspace_id), tenant_id_env_var="AZURE_TENANT_ID", client_id_env_var="AZURE_CLIENT_ID",
                                  client_secret_env_var="AZURE_CLIENT_SECRET")
    url = res.trigger_item_run(str(item_id), item_type, params or None)
    state = res.wait_for_run(url, max_wait_seconds=int(timeout_s), poll_interval=int(poll_s), log=SimpleNamespace(info=lambda m: ctx.log(f"[{ctx.pipeline}] {name}: {m}")))
    if state.get("status") != "Completed":
        raise AdfFail(f"{name}: Fabric {item_type} run ended {state.get('status')}: {state.get('failureReason') or state}")
    return Out({"status": "Completed", "jobInstanceId": state.get("id"), "runState": state})


def adf_mail(ctx: Ctx, name: str, to, subject, body, from_: str = "", cc="", bcc="", html: bool = True) -> Out:
    """Send an email over SMTP (SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM, SMTP_STARTTLS in the environment). ADF's Office 365 Outlook
    activity sends through a Microsoft connection; credentials are never copied, so the message goes through the SMTP server you configure instead."""
    ctx.log(f"[{ctx.pipeline}] {name}: mail to {to!r} subject {subject!r}" + (" (dry run, not sent)" if dry_run() else ""))
    if dry_run():
        return Out()
    import smtplib
    from email.message import EmailMessage
    host = os.environ.get("SMTP_HOST")
    if not host:
        raise AdfExprError("set SMTP_HOST (and SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM) to send mail")
    msg = EmailMessage()
    msg["From"] = from_ or os.environ.get("SMTP_FROM", "")
    msg["To"] = fx.string(to)
    if cc:
        msg["Cc"] = fx.string(cc)
    msg["Subject"] = fx.string(subject)
    text = fx.string(body)
    msg.set_content(text, subtype="html" if html else "plain")
    rcpt = [a.strip() for a in ",".join(x for x in (fx.string(to), fx.string(cc), fx.string(bcc)) if x).replace(";", ",").split(",") if a.strip()]
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=60) as s:
        if os.environ.get("SMTP_STARTTLS", "1") != "0":
            s.starttls()
        if os.environ.get("SMTP_USER"):
            s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
        s.send_message(msg, to_addrs=rcpt)
    return Out({"sent": True})


# ---------------------------------------------------------------------------------------------------------------------------------------------
# Copy, continued: format conversion (delimited text, JSON, Parquet, Excel) and the SQL families, through pandas, pyarrow and SQLAlchemy
# ---------------------------------------------------------------------------------------------------------------------------------------------

_EXT = {"DelimitedText": ".csv", "Json": ".json", "Parquet": ".parquet"}
_CODECS = {None: None, "": None, "none": None, "gzip": "gzip", "bzip2": "bz2"}


def _read_frame(fh, fmt: str, o: dict):
    """One file as a DataFrame. Delimited text and Excel are read as text (ADF reads them untyped unless a schema is mapped)."""
    import pandas as pd
    comp = _CODECS.get(o.get("compression"), None)
    if fmt == "DelimitedText":
        header = bool(o.get("header", False))
        df = pd.read_csv(fh, sep=o.get("delimiter", ","), quotechar=o.get("quote") or '"', header=0 if header else None, encoding=o.get("encoding", "utf-8"),
                         dtype=str, keep_default_na=False, na_values=[o.get("null_value", "")], compression=comp or "infer")
        if not header:
            df.columns = [f"Prop_{i}" for i in range(len(df.columns))]
        return df
    if fmt == "Parquet":
        return pd.read_parquet(fh)
    if fmt == "Json":
        text = fh.read().decode(o.get("encoding", "utf-8")).strip()
        rows = _json.loads(text) if text.startswith("[") else [_json.loads(x) for x in text.splitlines() if x.strip()]
        return pd.json_normalize(rows)
    if fmt == "Excel":
        header = bool(o.get("header", False))
        df = pd.read_excel(fh, sheet_name=o.get("sheet") if o.get("sheet") is not None else 0, header=0 if header else None, dtype=str,
                           keep_default_na=False, na_values=[o.get("null_value", "")])
        if not header:
            df.columns = [f"Prop_{i}" for i in range(len(df.columns))]
        return df
    raise AdfExprError(f"cannot read format {fmt}")


def _write_frame(df, dfs, target: str, fmt: str, o: dict) -> int:
    """Write a DataFrame as delimited text, JSON (one object per line, or an array) or Parquet; returns the bytes written."""
    import io
    import posixpath
    dfs.makedirs(posixpath.dirname(target), exist_ok=True)
    if fmt == "Parquet":
        buf = io.BytesIO()
        df.to_parquet(buf, engine="pyarrow", compression=o.get("compression") or "snappy", index=False)
        payload = buf.getvalue()
    elif fmt == "DelimitedText":
        payload = df.to_csv(sep=o.get("delimiter", ","), index=False, header=bool(o.get("header", True)), quotechar=o.get("quote") or '"',
                            lineterminator=o.get("row_delimiter") or "\r\n", na_rep=o.get("null_value", "")).encode(o.get("encoding", "utf-8"))
    elif fmt == "Json":
        payload = (df.to_json(orient="records") if o.get("array") else df.to_json(orient="records", lines=True)).encode(o.get("encoding", "utf-8"))
    else:
        raise AdfExprError(f"cannot write format {fmt}")
    with dfs.open(target, "wb") as w:
        w.write(payload)
    return len(payload)


def _apply_mapping(df, mapping):
    """The Copy activity's explicit column mapping: only the mapped source columns are copied, renamed to their sink names (names match case-sensitively)."""
    if not mapping:
        return df
    missing = [c for c in mapping if c not in df.columns]
    if missing:
        raise AdfExprError(f"the mapping names source column(s) the data does not have: {missing}")
    return df[list(mapping)].rename(columns=mapping)


def adf_copy_convert(ctx: Ctx, name: str, src_url, src_env: str, src_fmt: str, src_opts: dict, dst_url, dst_env: str, dst_fmt: str, dst_opts: dict,
                     dst_file: str = "", recursive: bool = True, wildcard: str = "", wildcard_folder: str = "", mapping: dict | None = None,
                     filters: dict | None = None, partition_root: str | None = None) -> Out:
    """Copy files between delimited text, JSON and Parquet (and from Excel), one output file per input file (named like the input, with the new extension),
    or all inputs merged into dst_file when it is given and there is more than one input."""
    ctx.log(f"[{ctx.pipeline}] {name}: convert {src_fmt} {src_url} -> {dst_fmt} {dst_url}" + (f"/{dst_file}" if dst_file else "") + (" (dry run, not copied)" if dry_run() else ""))
    if dry_run():
        return Out()
    import posixpath
    import pandas as pd
    sfs, spath = _fs(src_env, src_url)
    dfs, dpath = _fs(dst_env, dst_url)
    files, base = _find_files(sfs, spath, recursive, wildcard, wildcard_folder, filters)
    nbytes = nrows = 0
    frames = []
    for f in files:
        with sfs.open(f, "rb") as fh:
            df = _read_frame(fh, src_fmt, src_opts or {})
        if partition_root is not None:
            for k, v in _partition_columns(f, partition_root or base).items():
                df[k] = v
        df = _apply_mapping(df, mapping)
        nrows += len(df)
        if dst_file and len(files) > 1:
            frames.append(df)
            continue
        if dst_file:
            target = posixpath.join(dpath, dst_file)
        else:
            rel = posixpath.relpath(f, base) if base else f
            target = posixpath.join(dpath, posixpath.splitext(rel)[0] + _EXT[dst_fmt])
        nbytes += _write_frame(df, dfs, target, dst_fmt, dst_opts or {})
    if frames:
        nbytes += _write_frame(pd.concat(frames, ignore_index=True), dfs, posixpath.join(dpath, dst_file), dst_fmt, dst_opts or {})
    return _copy_out(len(files), nbytes, nrows)


def adf_copy_sql_to_parquet(ctx: Ctx, name: str, conn_env: str, query: str, dst_url, dst_env: str, dst_file: str, *, compression: str = "snappy", mapping=None,
                            params: dict | None = None, isolation: str = "") -> Out:
    """Run a query and write the rows to one Parquet file."""
    ctx.log(f"[{ctx.pipeline}] {name}: {query} on {conn_env} -> {dst_url}/{dst_file}" + (" (dry run, not executed)" if dry_run() else ""))
    if dry_run():
        return Out()
    import posixpath
    import pandas as pd
    url = os.environ.get(conn_env)
    if not url:
        raise AdfExprError(f"set {conn_env} to a SQLAlchemy URL for this linked service")
    from sqlalchemy import create_engine, text
    with _read_conn(create_engine(url), isolation) as con:
        df = _apply_mapping(pd.read_sql(text(query), con, params=params or None), mapping)
    dfs, dpath = _fs(dst_env, dst_url)
    n = _write_frame(df, dfs, posixpath.join(dpath, dst_file), "Parquet", {"compression": compression})
    return _copy_out(1, n, len(df))


def _sql_type(values):
    """A SQLAlchemy column type for the Python values seen (the sink table is created from the source, as tableOption autoCreate does)."""
    import datetime as _dt
    from sqlalchemy import BigInteger, Boolean, Date, DateTime, Float, Text
    v = next((x for x in values if x is not None), None)
    if isinstance(v, bool):
        return Boolean()
    if isinstance(v, int):
        return BigInteger()
    if isinstance(v, float):
        return Float()
    if isinstance(v, _dt.datetime):
        return DateTime()
    if isinstance(v, _dt.date):
        return Date()
    return Text()


def _sink_table(con, table_name: str, records: list, auto_create: bool):
    """The sink table, reflected. With tableOption autoCreate a missing table is created first, with a column per key of the first record."""
    from sqlalchemy import Column, MetaData, Table, inspect
    schema, tname = _split_table(table_name)
    if auto_create and not inspect(con).has_table(tname, schema=schema):
        cols = list(records[0]) if records else []
        Table(tname, MetaData(), *[Column(c, _sql_type([r.get(c) for r in records])) for c in cols], schema=schema).create(con)
    return Table(tname, MetaData(), autoload_with=con, schema=schema)


def _insert_records(con, table_name: str, records: list, batch: int = 1000, *, keys: list | None = None, auto_create: bool = False) -> int:
    """Insert rows into a table, or, with `keys` (upsertSettings.keys), update the row with the same key values and insert the rest. The upsert is a portable
    update-then-insert per row, so it works on any database SQLAlchemy reaches, at the cost of speed against a native MERGE."""
    from sqlalchemy import and_, insert, update
    if not records:
        return 0
    tbl = _sink_table(con, table_name, records, auto_create)
    if not keys:
        for i in range(0, len(records), batch):
            con.execute(insert(tbl), records[i:i + batch])
        return len(records)
    missing = [k for k in keys if k not in tbl.c]
    if missing:
        raise AdfExprError(f"upsert key column(s) {missing} are not in table {table_name}")
    for rec in records:
        where = and_(*[tbl.c[k] == rec.get(k) for k in keys])
        vals = {k: v for k, v in rec.items() if k not in keys}
        done = con.execute(update(tbl).where(where).values(**vals)).rowcount if vals else con.execute(tbl.select().where(where)).first() is not None
        if not done:
            con.execute(insert(tbl), [rec])
    return len(records)


def adf_copy_frames_to_sql(ctx: Ctx, name: str, src_url, src_env: str, src_fmt: str, src_opts: dict, conn_env: str, table, *, recursive: bool = True,
                           wildcard: str = "", wildcard_folder: str = "", pre_script: str = "", mapping: dict | None = None, batch: int = 1000,
                           keys: list | None = None, auto_create: bool = False, filters: dict | None = None, partition_root: str | None = None) -> Out:
    """Insert the rows of Parquet, JSON or Excel files into a table (the destination table must exist; its columns type the values)."""
    ctx.log(f"[{ctx.pipeline}] {name}: insert {src_fmt} {src_url} into {table} on {conn_env}" + (" (dry run, not executed)" if dry_run() else ""))
    if dry_run():
        return Out()
    url = os.environ.get(conn_env)
    if not url:
        raise AdfExprError(f"set {conn_env} to a SQLAlchemy URL for this linked service")
    from sqlalchemy import create_engine, text
    sfs, spath = _fs(src_env, src_url)
    files, base = _find_files(sfs, spath, recursive, wildcard, wildcard_folder, filters)
    rows_total = nbytes = 0
    with create_engine(url).begin() as con:
        if pre_script:
            con.execute(text(pre_script))
        for f in files:
            with sfs.open(f, "rb") as fh:
                df = _read_frame(fh, src_fmt, src_opts or {})
            nbytes += sfs.size(f) or 0
            if partition_root is not None:
                for k, v in _partition_columns(f, partition_root or base).items():
                    df[k] = v
            df = df.astype(object).where(df.notna(), None)
            df = _apply_mapping(df, mapping)
            rows_total += _insert_records(con, table, df.to_dict(orient="records"), batch, keys=keys, auto_create=auto_create)
    return _copy_out(len(files), nbytes, rows_total)


def _read_conn(engine, isolation: str = ""):
    """A connection for reading, at the isolation level a SQL source names (ReadCommitted, ReadUncommitted, RepeatableRead, Serializable, Snapshot)."""
    con = engine.connect()
    return con.execution_options(isolation_level=_ISOLATION.get(str(isolation).lower(), str(isolation))) if isolation else con


_ISOLATION = {"readcommitted": "READ COMMITTED", "readuncommitted": "READ UNCOMMITTED", "repeatableread": "REPEATABLE READ", "serializable": "SERIALIZABLE", "snapshot": "SNAPSHOT"}


def adf_copy_sql_to_sql(ctx: Ctx, name: str, src_env: str, query: str, dst_env: str, table, *, pre_script: str = "", mapping: dict | None = None,
                        batch: int = 10000, params: dict | None = None, isolation: str = "", keys: list | None = None, auto_create: bool = False) -> Out:
    """Stream the rows of a query (or stored procedure call, with its parameters) on one database into a table on another (or the same). preCopyScript runs first in
    the same transaction; with keys the rows are upserted; with auto_create a missing table is created from the first batch. Both ends are SQLAlchemy URLs from the
    environment, so any database SQLAlchemy reaches works with its driver installed."""
    ctx.log(f"[{ctx.pipeline}] {name}: {query} on {src_env} -> {table} on {dst_env}" + (" (dry run, not executed)" if dry_run() else ""))
    if dry_run():
        return Out()
    surl, durl = os.environ.get(src_env), os.environ.get(dst_env)
    for env, url in ((src_env, surl), (dst_env, durl)):
        if not url:
            raise AdfExprError(f"set {env} to a SQLAlchemy URL for this linked service")
    from sqlalchemy import create_engine, text
    total = 0
    opts = {"stream_results": True}
    if isolation:
        opts["isolation_level"] = _ISOLATION.get(str(isolation).lower(), str(isolation))
    with create_engine(surl).connect().execution_options(**opts) as src, create_engine(durl).begin() as dst:
        if pre_script:
            dst.execute(text(pre_script))
        res = src.execute(text(query), params or {})
        cols = list(res.keys())
        while True:
            chunk = res.fetchmany(batch)
            if not chunk:
                break
            recs = [dict(zip(cols, r)) for r in chunk]
            if mapping:
                recs = [{mapping[k]: v for k, v in r.items() if k in mapping} for r in recs]
            total += _insert_records(dst, table, recs, batch, keys=keys, auto_create=auto_create)
    return _copy_out(0, 0, total)


def schedule_trigger(name: str, context) -> dict:
    """What a schedule-triggered ADF run can read from trigger(): scheduledTime (when the schedule meant it to start) and startTime (when it did), both UTC ISO 8601.
    A Dagster schedule tick carries its scheduled time in a run tag; any other run (a person launching it) reads as a manual run with both times now."""
    tags = getattr(getattr(context, "run", None), "tags", None) or {}
    now = _iso(datetime.now(timezone.utc))
    at, sched = tags.get(".dagster/scheduled_execution_time"), tags.get("dagster/schedule_name")
    if at and sched:
        return {"type": "ScheduleTrigger", "name": sched, "scheduledTime": _iso(_parse_dt(at).astimezone(timezone.utc)), "startTime": now}
    return {"type": "Manual", "name": "Manual", "scheduledTime": now, "startTime": now}


def adf_script(ctx: Ctx, name: str, conn_env: str, blocks: list) -> Out:
    """The Script activity. blocks: [{"type": "Query" | "NonQuery", "text": sql, "params": {name: value}}] run in order in one transaction. A parameter is referenced
    in the text as @name (the documentation says each must be referenced explicitly) and is bound, never pasted in. A Query block adds a result set
    ({rowCount, rows}, at most 5000 rows and 4 MB like Lookup), a NonQuery block adds to recordsAffected. Only the first result set of a block is read, and output
    parameters are not supported."""
    ctx.log(f"[{ctx.pipeline}] {name}: script of {len(blocks)} block(s) on {conn_env}" + (" (dry run, not executed)" if dry_run() else ""))
    if dry_run():
        return Out()
    url = os.environ.get(conn_env)
    if not url:
        raise AdfExprError(f"set {conn_env} to a SQLAlchemy URL for this linked service")
    from sqlalchemy import create_engine, text
    sets, affected, saw_nonquery = [], 0, False
    with create_engine(url).begin() as con:
        for b in blocks:
            sql, params = str(b.get("text") or ""), dict(b.get("params") or {})
            for pn in params:
                sql = re.sub(r"(?<![@\w])@" + re.escape(pn) + r"\b", ":" + pn, sql)
            res = con.execute(text(sql), params)
            if str(b.get("type", "Query")).lower() == "nonquery":
                saw_nonquery = True
                affected += max(res.rowcount or 0, 0)
            else:
                rows = _lookup_limits(name, [dict(r._mapping) for r in res] if res.returns_rows else [])
                sets.append({"rowCount": len(rows), "rows": rows})
    out = Out({"resultSetCount": len(sets), "resultSets": sets})
    if saw_nonquery:
        out["recordsAffected"] = affected
    return out


# ---------------------------------------------------------------------------------------------------------------------------------------------
# Mapping Data Flow: the flow's script runs row by row in this process (dflow.py); sources and sinks are files (delimited text, Parquet, JSON through fsspec)
# and SQL tables or queries (SQLAlchemy URLs from the environment, as for Copy).
# ---------------------------------------------------------------------------------------------------------------------------------------------

def df_param(src: str):
    """The value of a data flow parameter written as a data flow expression (a quoted string, a number, toDate('...'), ...)."""
    from . import dflow
    return dflow.compile_expr(src)({}, {})


def _frame_records(df) -> list:
    df = df.astype(object).where(df.notna(), None)
    return df.to_dict(orient="records")


def _write_policies(con, spec: dict, rows: list) -> int:
    """Write a data flow's rows to a table. An alterRow transformation marks each row insert, update, delete or upsert (a row with no mark is an insert); the sink
    applies only the policies it allows, matching on its key columns. Returns the rows written."""
    from sqlalchemy import and_, delete, update
    allow, keys, table = spec.get("allow") or {"insert": True}, spec.get("keys") or [], spec["table"]
    groups: dict[str, list] = {"insert": [], "update": [], "delete": [], "upsert": []}
    cols = None
    if rows:
        try:
            cols = {c.lower(): c for c in _sink_table(con, table, rows[:1], False).c.keys()}          # a sink maps columns by name; columns the table lacks are not written
        except Exception:                                                                       # noqa: BLE001  (a missing table surfaces when inserting)
            cols = None
    for r in rows:
        rec = {k: v for k, v in r.items() if not k.startswith("__")}
        if cols is not None:
            rec = {cols[k.lower()]: v for k, v in rec.items() if k.lower() in cols}
        groups[r.get("__alterRow__", "insert")].append(rec)
    n = 0
    if allow.get("insert") and groups["insert"]:
        n += _insert_records(con, table, groups["insert"])
    if allow.get("upsert") and groups["upsert"]:
        n += _insert_records(con, table, groups["upsert"], keys=keys)
    if (allow.get("update") and groups["update"]) or (allow.get("delete") and groups["delete"]):
        tbl = _sink_table(con, table, groups["update"] + groups["delete"], False)
        for r in groups["update"] if allow.get("update") else []:
            vals = {k: v for k, v in r.items() if k not in keys}
            if vals:
                con.execute(update(tbl).where(and_(*[tbl.c[k] == r.get(k) for k in keys])).values(**vals))
            n += 1
        for r in groups["delete"] if allow.get("delete") else []:
            con.execute(delete(tbl).where(and_(*[tbl.c[k] == r.get(k) for k in keys])))
            n += 1
    return n


def adf_dataflow(ctx: Ctx, name: str, flow: str, script: str, sources: dict, sinks: dict, params: dict | None = None) -> Out:
    """Run a Mapping Data Flow. The result has the metrics the activity reports: runStatus.metrics.<sink>.rowsWritten."""
    ctx.log(f"[{ctx.pipeline}] {name}: data flow {flow}" + (" (dry run, not executed)" if dry_run() else ""))
    if dry_run():
        return Out({"runStatus": {"status": "Succeeded", "metrics": {k: {"rowsWritten": 0} for k in sinks}}})
    from . import dflow
    import posixpath

    class IO:
        def read(self, sname, schema):
            spec = sources[sname]
            if spec["kind"] == "sql":
                url = os.environ.get(spec["env"])
                if not url:
                    raise AdfExprError(f"set {spec['env']} to a SQLAlchemy URL for this linked service")
                from sqlalchemy import create_engine, text
                with _read_conn(create_engine(url), spec.get("isolation", "")) as con:
                    res = con.execute(text(spec["query"]))
                    cols = list(res.keys())
                    rows = [dict(zip(cols, r)) for r in res.fetchall()]
            else:
                fs, path = _fs(spec["env"], spec["url"])
                files, _ = _find_files(fs, path, False, spec.get("file", ""), "", None)
                rows = []
                for f in files:
                    with fs.open(f, "rb") as fh:
                        if spec["fmt"] == "Json":                      # nested objects and arrays stay nested: a flatten transformation unrolls them
                            text = fh.read().decode(spec["opts"].get("encoding", "utf-8")).strip()
                            rows.extend(_json.loads(text) if text.startswith("[") else [_json.loads(x) for x in text.splitlines() if x.strip()])
                        else:
                            rows.extend(_frame_records(_read_frame(fh, spec["fmt"], spec["opts"])))
                if spec["fmt"] == "DelimitedText" and not spec["opts"].get("header") and schema:
                    names = [c for c, _ in schema]                      # no header row: the projection names the columns by position
                    rows = [{names[i]: v for i, v in enumerate(r.values()) if i < len(names)} for r in rows]
                if spec["fmt"] == "DelimitedText":
                    types = dict(schema)
                    rows = [{k: dflow.cast_value(v, types[k]) if k in types else v for k, v in r.items()} for r in rows]
            ctx.log(f"[{ctx.pipeline}] {name}: source {sname} read {len(rows)} row(s)")
            return rows

        def write(self, kname, rows, opts):
            spec = sinks[kname]
            if spec["kind"] == "sql":
                url = os.environ.get(spec["env"])
                if not url:
                    raise AdfExprError(f"set {spec['env']} to a SQLAlchemy URL for this linked service")
                from sqlalchemy import create_engine, text
                with create_engine(url).begin() as con:
                    for stmt in spec.get("pre_sql") or []:
                        con.execute(text(stmt))
                    if spec.get("truncate"):
                        con.execute(text(f"DELETE FROM {spec['table']}"))
                    return _write_policies(con, spec, rows)
            import pandas as pd
            fs, path = _fs(spec["env"], spec["url"])
            if spec.get("truncate") and fs.exists(path):
                for f in fs.ls(path, detail=False):
                    if fs.isfile(f):
                        fs.rm(f)
            target = spec["file"] or f"part-00000-{uuid.uuid4()}-c000{_EXT.get(spec['fmt'], '')}"        # Spark's naming when the sink names no file
            rows = [{k: v for k, v in r.items() if not k.startswith("__")} for r in rows]
            _write_frame(pd.DataFrame(rows), fs, posixpath.join(path, target), spec["fmt"], spec["opts"])
            return len(rows)

    written = dflow.run_flow(script, IO(), {k: v for k, v in (params or {}).items()})
    for k, v in written.items():
        ctx.log(f"[{ctx.pipeline}] {name}: sink {k} wrote {v} row(s)")
    return Out({"runStatus": {"status": "Succeeded", "metrics": {k: {"rowsWritten": v} for k, v in written.items()}}})
