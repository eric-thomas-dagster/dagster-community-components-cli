"""Mapping Data Flows: the data flow expression language, the data flow script, and a small row-wise executor.

Scope, deliberately narrow: sources and sinks over files (delimited text, JSON, Parquet) and SQL, and the transformations source, select, filter, derive, sort,
aggregate, join (equality conditions), union and sink. A flow that uses anything else (alterRow, lookup, window, pivot, flatten, parse, ...) is a stub: the
converter calls `check_flow` and refuses it with the reason. Everything runs row by row over lists of dicts, which is simple and exact rather than fast.

The expression language follows Microsoft's data flow function reference (tests/data/adf-data-flow-expressions-usage.md): arithmetic and comparison propagate null,
strings concatenate with + or concat, substring is 1-based, round defaults to half up, and so on. Each documented example of an implemented function is a test.
This module is copied into generated projects and imports their runtime for file and SQL access.
"""
from __future__ import annotations

import datetime as _dt
import decimal as _dec
import json as _json
import math
import re
from typing import Any, Callable


class Unsupported(Exception):
    """Something in a data flow this translator does not handle: the whole flow becomes a loud stub."""


# ---------------------------------------------------------------------------------------------------------------------------------------------
# expression language
# ---------------------------------------------------------------------------------------------------------------------------------------------

_TOK = re.compile(r"""\s*(?:
    (?P<num>\d+\.\d+|\.\d+|\d+)
  | (?P<str>'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")
  | (?P<qcol>\{[^}]*\})
  | (?P<param>\$[A-Za-z_][\w]*)
  | (?P<ident>[A-Za-z_][\w]*(?:@[A-Za-z_][\w]*|@\{[^}]*\})?)
  | (?P<op><=>|==|!=|<>|<=|>=|&&|\|\||[-+*/%<>!(),\[\].@:])
)""", re.X)


def _tokens(src: str):
    out, i = [], 0
    src = src.strip()
    while i < len(src):
        m = _TOK.match(src, i)
        if not m or m.end() == i:
            raise Unsupported(f"cannot read {src[i:i + 20]!r} in the expression {src!r}")
        i = m.end()
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
    return out


AGG = {"sum", "count", "min", "max", "avg", "first", "last", "countdistinct", "sumdistinct", "stddev", "variance"}


class _Parser:
    """Parses a data flow expression into a small tree: ('num', text), ('str', text), ('lit', True | False | None), ('col', name, stream), ('param', name),
    ('bin', op, left, right), ('not', x), ('neg', x) and ('call', lower-case name, [args], name as written). `emit_py` turns the tree into Python for the executor;
    `dflow_sql.emit_sql` turns the same tree into SQL for the warehouse components."""

    def __init__(self, src: str):
        self.src, self.t, self.i = src, _tokens(src), 0

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def eat(self, val=None, kind=None):
        k, v = self.peek()
        if (val is not None and v == val) or (kind is not None and k == kind):
            self.i += 1
            return v
        return None

    def need(self, val):
        if self.eat(val) is None:
            raise Unsupported(f"expected {val!r} in the expression {self.src!r}")

    def parse(self):
        e = self.or_()
        if self.i != len(self.t):
            raise Unsupported(f"unexpected {self.peek()[1]!r} in the expression {self.src!r}")
        return e

    def or_(self):
        e = self.and_()
        while self.eat("||"):
            e = ("bin", "||", e, self.and_())
        return e

    def and_(self):
        e = self.cmp()
        while self.eat("&&"):
            e = ("bin", "&&", e, self.cmp())
        return e

    def cmp(self):
        e = self.add()
        while True:
            k, v = self.peek()
            if v in ("==", "!=", "<>", "<=", ">=", "<", ">", "<=>") and k == "op":
                self.i += 1
                e = ("bin", v, e, self.add())
            else:
                return e

    def add(self):
        e = self.mul()
        while True:
            k, v = self.peek()
            if k == "op" and v in ("+", "-"):
                self.i += 1
                e = ("bin", v, e, self.mul())
            else:
                return e

    def mul(self):
        e = self.unary()
        while True:
            k, v = self.peek()
            if k == "op" and v in ("*", "/", "%"):
                self.i += 1
                e = ("bin", v, e, self.unary())
            else:
                return e

    def unary(self):
        if self.eat("!"):
            return ("not", self.unary())
        if self.eat("-"):
            return ("neg", self.unary())
        return self.primary()

    def primary(self):
        k, v = self.peek()
        if k == "num":
            self.i += 1
            return ("num", v)
        if k == "str":
            self.i += 1
            return ("str", bytes(v[1:-1], "utf-8").decode("unicode_escape") if "\\" in v else v[1:-1])
        if k == "qcol":
            self.i += 1
            return ("col", v[1:-1], None)
        if k == "param":
            self.i += 1
            return ("param", v[1:])
        if k == "op" and v == "(":
            self.i += 1
            e = self.or_()
            self.need(")")
            return e
        if k == "ident":
            self.i += 1
            if self.eat("("):
                args = []
                if not self.eat(")"):
                    while True:
                        args.append(self.or_())
                        if self.eat(")"):
                            break
                        self.need(",")
                return ("call", v.lower(), args, v)
            low = v.lower()
            if low in ("true", "false"):
                return ("lit", low == "true")
            if low == "null":
                return ("lit", None)
            if "@" in v:
                stream, col = v.split("@", 1)
                return ("col", col.strip("{}"), stream)
            return ("col", v, None)
        raise Unsupported(f"unexpected {v!r} in the expression {self.src!r}")


_BIN_PY = {"||": "or_", "&&": "and_", "+": "add", "-": "sub", "*": "mul", "/": "div", "%": "mod"}


class _PyEmit:
    """The tree as Python source over a row `r`, the parameters `P`, the group's rows `rows` and the window position `w`."""

    def __init__(self, agg_ok: bool, win: bool = False):
        self.agg_ok, self.win, self.analytic = agg_ok or win, win, False

    def emit(self, n) -> str:
        k = n[0]
        if k == "num":
            return f"D.num({n[1]!r})"
        if k == "str":
            return repr(n[1])
        if k == "lit":
            return repr(n[1])
        if k == "col":
            return f"D.col(r, {n[1]!r}, {n[2]!r})" if n[2] else f"D.col(r, {n[1]!r})"
        if k == "param":
            return f"D.param(P, {n[1]!r})"
        if k == "not":
            return f"D.not_({self.emit(n[1])})"
        if k == "neg":
            return f"D.neg({self.emit(n[1])})"
        if k == "bin":
            op = n[1]
            l, r = self.emit(n[2]), self.emit(n[3])
            return f"D.{_BIN_PY[op]}({l}, {r})" if op in _BIN_PY else f"D.cmp({op!r}, {l}, {r})"
        return self.call(n[3], n[1], n[2])

    def call(self, name: str, low: str, args: list) -> str:
        code = [self.emit(a) for a in args]
        if low in AGG:
            if not self.agg_ok:
                raise Unsupported(f"{name}() outside an aggregate transformation")
            if low == "count" and not args:
                return "G.count(rows, lambda r: 1)"
            return f"G.{low}(rows, lambda r: {code[0]})"
        if low in ("rank", "denserank", "rownumber", "cumedist", "ntile", "lag", "lead"):
            if not self.win:
                raise Unsupported(f"{name}() outside a window transformation")
            self.analytic = True
            if low in ("rank", "denserank", "rownumber", "cumedist"):
                if args:
                    raise Unsupported(f"{name}() takes no arguments")
                return f"W.{low}(w)"
            if low == "ntile":
                return f"W.ntile(w, {code[0] if code else 'None'})"
            if not args:
                raise Unsupported(f"{name}() needs a value")
            return f"W.shift(w, lambda r: {code[0]}, {code[1] if len(code) > 1 else '1'}, {code[2] if len(code) > 2 else 'None'}, {-1 if low == 'lag' else 1})"
        if low == "ismatch":
            if args:
                raise Unsupported("isMatch() with a stream argument is not translated")
            return "D.ismatch(r)"
        if low == "iif":
            return f"D.iif(lambda: {code[0]}, lambda: {code[1]}, lambda: {code[2] if len(code) > 2 else 'None'})"
        if low == "case":
            return "D.case([" + ", ".join(f"lambda: {a}" for a in code) + "])"
        if low in ("iifnull", "coalesce"):
            return "D.coalesce([" + ", ".join(f"lambda: {a}" for a in code) + "])"
        if not hasattr(D, "f_" + low):
            raise Unsupported(f"the function {name}() is not translated")
        check_call_args(name, low, args)
        return f"D.f_{low}({', '.join(code)})"


def check_call_args(name: str, low: str, args: list) -> None:
    """Refuse the calls whose format, locale or time-zone arguments are not implemented, whichever backend runs them: never guess."""
    if low in ("tostring", "tointeger", "tolong", "toshort", "todouble", "tofloat") and len(args) > 1:
        raise Unsupported(f"{name}() with a format argument is not translated")
    if low == "todecimal" and len(args) > 3:
        raise Unsupported("toDecimal() with a format argument is not translated")
    if low in ("todate", "totimestamp") and len(args) > 1:
        fmt = args[1]
        if not (fmt[0] == "str" and re.fullmatch(r"[yMdHms\-/:. T]*", fmt[1])):
            raise Unsupported(f"{name}() with a date pattern that is not a plain literal of y, M, d, H, m, s is not translated")
        if len(args) > 2:
            raise Unsupported(f"{name}() with a time zone is not translated")


def parse_expr(src: str):
    """The expression tree for a data flow expression."""
    return _Parser(src).parse()


def compile_expr(src: str, agg: bool = False, win: bool = False) -> Callable:
    """Compile a data flow expression to a function of a row (r), the flow parameters (P), the group's rows (in an aggregate or a window) and the window position
    (w). In a window the analytic functions (rank, lag, ...) are available; `fn.analytic` says whether the expression uses one."""
    tree = parse_expr(src)
    emitter = _PyEmit(agg, win)
    code = emitter.emit(tree)
    fn = eval(f"lambda r, P, rows=None, w=None: {code}", {"D": D, "G": G, "W": W})
    fn.source, fn.analytic, fn.tree = code, emitter.analytic, tree
    return fn


# ---------------------------------------------------------------------------------------------------------------------------------------------
# scalar functions: null in, null out, as the reference's examples show
# ---------------------------------------------------------------------------------------------------------------------------------------------

def _num(x):
    if isinstance(x, bool) or x is None:
        return x
    return x


class _Fns:
    # --- operators
    @staticmethod
    def num(text):
        return int(text) if "." not in text else _dec.Decimal(text)

    @staticmethod
    def col(r, name, stream=None):
        if stream and f"{stream}@{name}" in r:
            return r[f"{stream}@{name}"]
        if name in r:
            return r[name]
        low = name.lower()
        for k in r:
            if k.lower() == low:                                   # column names are not case-sensitive in data flows by default
                return r[k]
        raise KeyError(f"the data flow has no column {name!r}")

    @staticmethod
    def ismatch(r):
        return bool(r.get("__isMatch__", False))

    @staticmethod
    def col_or_none(r, name):
        low = name.lower()
        for k in r:
            if k.lower() == low:
                return r[k]
        return None

    @staticmethod
    def param(P, name):
        if name not in P:
            raise KeyError(f"the data flow has no parameter ${name}")
        return P[name]

    @staticmethod
    def add(a, b):
        if a is None or b is None:
            return None
        if isinstance(a, str) or isinstance(b, str):
            return str(a) + str(b)
        return a + b

    @staticmethod
    def sub(a, b):
        return None if a is None or b is None else a - b

    @staticmethod
    def mul(a, b):
        return None if a is None or b is None else a * b

    @staticmethod
    def div(a, b):
        if a is None or b is None:
            return None
        if isinstance(a, int) and isinstance(b, int) and not isinstance(a, bool):
            return a // b if b and a % b == 0 else (a / b if b else None)
        return a / b if b else None

    @staticmethod
    def mod(a, b):
        return None if a is None or b is None or b == 0 else math.fmod(a, b) if isinstance(a, float) or isinstance(b, float) else a % b

    @staticmethod
    def neg(a):
        return None if a is None else -a

    @staticmethod
    def not_(a):
        return None if a is None else not a

    @staticmethod
    def and_(a, b):
        if a is False or b is False:
            return False
        return None if a is None or b is None else True

    @staticmethod
    def or_(a, b):
        if a is True or b is True:
            return True
        return None if a is None or b is None else False

    @staticmethod
    def cmp(op, a, b):
        if op == "<=>":                                            # the reference defines <=> as equalsIgnoreCase; two nulls are equal (Spark's null-safe equality)
            return a.lower() == b.lower() if isinstance(a, str) and isinstance(b, str) else a == b
        if a is None or b is None:
            return None
        return {"==": a == b, "!=": a != b, "<>": a != b, "<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op]

    @staticmethod
    def iif(c, t, f):
        v = c()
        return t() if v else f()

    @staticmethod
    def case(parts):
        i = 0
        while i + 1 < len(parts):
            if parts[i]():
                return parts[i + 1]()
            i += 2
        return parts[i]() if i < len(parts) else None

    @staticmethod
    def coalesce(parts):
        for p in parts:
            v = p()
            if v is not None:
                return v
        return None

    # --- functions (f_<lower-case name>)
    @staticmethod
    def f_isnull(a): return a is None
    @staticmethod
    def f_upper(s): return None if s is None else str(s).upper()
    @staticmethod
    def f_lower(s): return None if s is None else str(s).lower()
    @staticmethod
    def f_trim(s, chars=None): return None if s is None else str(s).strip(chars)
    @staticmethod
    def f_ltrim(s, chars=None): return None if s is None else str(s).lstrip(chars)
    @staticmethod
    def f_rtrim(s, chars=None): return None if s is None else str(s).rstrip(chars)
    @staticmethod
    def f_length(s): return None if s is None else len(s)
    @staticmethod
    def f_concat(*a): return None if any(x is None for x in a) else "".join(str(x) for x in a)
    @staticmethod
    def f_concatws(sep, *a): return None if sep is None or any(x is None for x in a) else str(sep).join(str(x) for x in a)
    @staticmethod
    def f_replace(s, find, rep=""): return None if s is None or find is None else str(s).replace(find, "" if rep is None else rep)

    @staticmethod
    def f_substring(s, start, n=None):
        if s is None or start is None:
            return None
        i = max(int(start) - 1, 0)                                 # 1-based
        return s[i:] if n is None else s[i:i + int(n)]

    @staticmethod
    def f_left(s, n): return None if s is None else s[:int(n)]
    @staticmethod
    def f_right(s, n): return None if s is None else s[-int(n):] if int(n) else ""

    @staticmethod
    def f_substringindex(s, delim, n):
        if s is None:
            return None
        parts = s.split(delim)
        n = int(n)
        return delim.join(parts[:n]) if n > 0 else delim.join(parts[n:])

    @staticmethod
    def f_abs(a): return None if a is None else abs(a)
    @staticmethod
    def f_ceil(a): return None if a is None else math.ceil(a)
    @staticmethod
    def f_floor(a): return None if a is None else math.floor(a)
    @staticmethod
    def f_add(a, b): return D.add(a, b)
    @staticmethod
    def f_minus(a, b): return D.sub(a, b)
    @staticmethod
    def f_multiply(a, b): return D.mul(a, b)
    @staticmethod
    def f_divide(a, b): return D.div(a, b)
    @staticmethod
    def f_mod(a, b): return D.mod(a, b)

    @staticmethod
    def f_round(a, scale=0, mode=5):
        if a is None:
            return None
        q = _dec.Decimal(1).scaleb(-int(scale))
        rm = {1: _dec.ROUND_UP, 2: _dec.ROUND_DOWN, 3: _dec.ROUND_CEILING, 4: _dec.ROUND_FLOOR, 5: _dec.ROUND_HALF_UP, 6: _dec.ROUND_HALF_DOWN, 7: _dec.ROUND_HALF_EVEN}[int(mode)]
        return float(_dec.Decimal(str(a)).quantize(q, rounding=rm))

    @staticmethod
    def f_tostring(a, fmt=None):
        if a is None:
            return None
        if isinstance(a, bool):
            return "true" if a else "false"
        if isinstance(a, _dt.datetime):
            return a.strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(a, _dt.date):
            return a.isoformat()
        return str(a)

    @staticmethod
    def f_tointeger(a, *fmt): return None if a is None else int(float(a)) if not isinstance(a, bool) else int(a)
    @staticmethod
    def f_tolong(a, *fmt): return D.f_tointeger(a)
    @staticmethod
    def f_toshort(a, *fmt): return D.f_tointeger(a)
    @staticmethod
    def f_todouble(a, *fmt): return None if a is None else float(a)
    @staticmethod
    def f_tofloat(a, *fmt): return D.f_todouble(a)

    @staticmethod
    def f_todecimal(a, precision=None, scale=None, *fmt):
        if a is None:
            return None
        v = _dec.Decimal(str(a))
        return v.quantize(_dec.Decimal(1).scaleb(-int(scale))) if scale is not None else v

    @staticmethod
    def f_toboolean(a):
        if a is None:
            return None
        if isinstance(a, str):
            w = a.strip().lower()
            return True if w in ("t", "true", "y", "yes", "1") else False if w in ("f", "false", "n", "no", "0") else None     # anything else is null
        return bool(a)

    @staticmethod
    def f_locate(sub, s, start=None):
        if sub is None or s is None:
            return None
        i = str(s).find(str(sub), (start or 1) - 1)
        return i + 1 if i >= 0 else 0
    @staticmethod
    def f_not(a): return D.not_(a)
    @staticmethod
    def f_equals(a, b): return D.cmp("==", a, b)
    @staticmethod
    def f_equalsignorecase(a, b): return D.cmp("<=>", a, b)
    @staticmethod
    def f_true(): return True
    @staticmethod
    def f_false(): return False
    @staticmethod
    def f_uuid(): return str(__import__("uuid").uuid4())

    @staticmethod
    def f_todate(a, fmt=None):
        if a is None:
            return None
        if isinstance(a, _dt.datetime):
            return a.date()
        if isinstance(a, _dt.date):
            return a
        s = str(a)
        if fmt:
            try:
                return _dt.datetime.strptime(s, _java_fmt(fmt)).date()
            except ValueError:
                return None                      # data flows give null for text that does not match the pattern
        m = re.match(r"\s*(\d{4})-(\d{1,2})-(\d{1,2})", s)
        if not m:
            raise ValueError(f"cannot read {s!r} as a date")
        return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    @staticmethod
    def f_totimestamp(a, fmt=None, *tz):
        if a is None:
            return None
        if isinstance(a, _dt.datetime):
            return a
        s = str(a)
        if fmt:
            try:
                return _dt.datetime.strptime(s, _java_fmt(fmt))
            except ValueError:
                return None
        return _dt.datetime.fromisoformat(s.replace("Z", ""))

    @staticmethod
    def f_currentdate(*tz): return _dt.date.today()
    @staticmethod
    def f_currenttimestamp(): return _dt.datetime.utcnow()
    @staticmethod
    def f_year(d): return None if d is None else D.f_todate(d).year
    @staticmethod
    def f_month(d): return None if d is None else D.f_todate(d).month
    @staticmethod
    def f_dayofmonth(d): return None if d is None else D.f_todate(d).day
    @staticmethod
    def f_dayofweek(d): return None if d is None else (D.f_todate(d).isoweekday() % 7) + 1          # 1 is Sunday, 7 is Saturday
    @staticmethod
    def f_dayofyear(d): return None if d is None else D.f_todate(d).timetuple().tm_yday
    @staticmethod
    def f_adddays(d, n): return None if d is None or n is None else D.f_todate(d) + _dt.timedelta(days=int(n))

    @staticmethod
    def f_addmonths(d, n):
        if d is None or n is None:
            return None
        import calendar
        x = D.f_todate(d)
        m0 = x.month - 1 + int(n)
        y, m = x.year + m0 // 12, m0 % 12 + 1
        return x.replace(year=y, month=m, day=min(x.day, calendar.monthrange(y, m)[1]))


def _java_fmt(f: str) -> str:
    """The common Java date pattern letters as strptime codes."""
    for a, b in (("yyyy", "%Y"), ("yy", "%y"), ("MM", "%m"), ("dd", "%d"), ("HH", "%H"), ("mm", "%M"), ("ss", "%S")):
        f = f.replace(a, b)
    return f


D = _Fns


# ---------------------------------------------------------------------------------------------------------------------------------------------
# aggregate functions over a group's rows: nulls are skipped
# ---------------------------------------------------------------------------------------------------------------------------------------------

class _Agg:
    @staticmethod
    def _vals(rows, f): return [v for v in (f(r) for r in rows) if v is not None]
    @staticmethod
    def sum(rows, f):
        v = G._vals(rows, f)
        return sum(v) if v else None
    @staticmethod
    def count(rows, f): return len(G._vals(rows, f))
    @staticmethod
    def min(rows, f):
        v = G._vals(rows, f)
        return min(v) if v else None
    @staticmethod
    def max(rows, f):
        v = G._vals(rows, f)
        return max(v) if v else None
    @staticmethod
    def avg(rows, f):
        v = G._vals(rows, f)
        return (sum(v) / len(v)) if v else None
    @staticmethod
    def first(rows, f): return f(rows[0]) if rows else None
    @staticmethod
    def last(rows, f): return f(rows[-1]) if rows else None
    @staticmethod
    def countdistinct(rows, f): return len(set(G._vals(rows, f)))
    @staticmethod
    def sumdistinct(rows, f): return sum(set(G._vals(rows, f)))
    @staticmethod
    def stddev(rows, f):
        v = [float(x) for x in G._vals(rows, f)]
        if len(v) < 2:
            return None
        m = sum(v) / len(v)
        return math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1))
    @staticmethod
    def variance(rows, f):
        s = G.stddev(rows, f)
        return None if s is None else s * s


G = _Agg


class _Win:
    """A row's place in its ordered window partition: the rows, its index and each row's sort-key tuple (ties share a key)."""
    def __init__(self, rows, i, keys):
        self.rows, self.i, self.keys = rows, i, keys


class _WinFns:
    @staticmethod
    def rownumber(w): return w.i + 1
    @staticmethod
    def rank(w):
        k = w.keys[w.i]
        return next(j for j, x in enumerate(w.keys) if x == k) + 1                 # one plus the rows before the first of its ties: gaps follow a tie
    @staticmethod
    def denserank(w):
        k = w.keys[w.i]
        seen, n = [], 0
        for x in w.keys:
            if not seen or x != seen[-1]:
                n += 1
                seen.append(x)
            if x == k:
                return n
    @staticmethod
    def cumedist(w):
        k = w.keys[w.i]
        last = max(j for j, x in enumerate(w.keys) if x == k)                       # rows up to and including the last of its ties, in the window's order
        return (last + 1) / len(w.keys)
    @staticmethod
    def ntile(w, n):
        n = int(n or 1)
        total, base, extra = len(w.rows), len(w.rows) // n, len(w.rows) % n          # the remainder goes one per bucket, from the first bucket
        edge, bucket = 0, 1
        for bucket in range(1, n + 1):
            edge += base + (1 if bucket <= extra else 0)
            if w.i < edge:
                return bucket
        return n
    @staticmethod
    def shift(w, f, n, default, direction):
        j = w.i + direction * int(n)
        return f(w.rows[j]) if 0 <= j < len(w.rows) else default


W = _WinFns


# ---------------------------------------------------------------------------------------------------------------------------------------------
# the data flow script: parameters{...} then `<inputs> kind(args) ~> Name` statements (tests/data/adf-data-flow-script.md)
# ---------------------------------------------------------------------------------------------------------------------------------------------

class Node:
    def __init__(self, name: str, kind: str, inputs: list[str], args: list[str], outputs: list[str] | None = None):
        self.name, self.kind, self.inputs, self.args, self.outputs = name, kind, inputs, args, outputs or []
        self.opts: dict[str, str] = {}      # `key: value` arguments, value as written
        self.pos: list[str] = []            # the other arguments
        for a in args:
            m = re.match(r"^\s*([A-Za-z_]\w*)\s*:\s*(.*)$", a, re.S)
            if m:
                self.opts[m.group(1)] = m.group(2).strip()
            else:
                self.pos.append(a.strip())

    def __repr__(self):
        return f"Node({self.kind} {self.name} <- {self.inputs})"


def _split_top(s: str, sep: str = ",") -> list[str]:
    """Split on a separator outside quotes, brackets, parentheses and braces."""
    out, depth, cur, i, q = [], 0, [], 0, None
    while i < len(s):
        c = s[i]
        if q:
            cur.append(c)
            if c == "\\" and i + 1 < len(s):
                cur.append(s[i + 1])
                i += 1
            elif c == q:
                q = None
        elif c in "'\"":
            q = c
            cur.append(c)
        elif c in "([{":
            depth += 1
            cur.append(c)
        elif c in ")]}":
            depth -= 1
            cur.append(c)
        elif c == sep and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    tail = "".join(cur)
    if tail.strip() or out:
        out.append(tail)
    return [x.strip() for x in out if x.strip()]


def _balanced(s: str, start: int, open_: str, close: str) -> int:
    """Index just after the bracket that closes the one at `start`."""
    depth, q, i = 0, None, start
    while i < len(s):
        c = s[i]
        if q:
            if c == "\\":
                i += 1
            elif c == q:
                q = None
        elif c in "'\"":
            q = c
        elif c == open_:
            depth += 1
        elif c == close:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise Unsupported("the data flow script has unbalanced brackets")


def parse_script(text: str) -> tuple[dict[str, tuple[str, str | None]], list[Node]]:
    """({parameter: (type, default expression source)}, [Node...]) from a data flow script."""
    text = re.sub(r"//[^\n]*", "", text) if "//" in text and "'//" not in text else text
    text = text.strip()
    params: dict[str, tuple[str, str | None]] = {}
    m = re.match(r"parameters\s*\{", text)
    if m:
        end = _balanced(text, m.end() - 1, "{", "}")
        for item in _split_top(text[m.end():end - 1]):
            pm = re.match(r"^(\w+)\s+as\s+(\w+(?:\[\])?)\s*(?:\((.*)\))?\s*$", item, re.S)
            if not pm:
                raise Unsupported(f"cannot read the parameter {item!r}")
            params[pm.group(1)] = (pm.group(2), pm.group(3))
        text = text[end:].strip()
    nodes: list[Node] = []
    i, depth, q, start = 0, 0, None, 0
    while i < len(text):
        c = text[i]
        if q:
            if c == "\\":
                i += 1
            elif c == q:
                q = None
        elif c in "'\"":
            q = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif depth == 0 and text.startswith("~>", i):
            body = text[start:i].strip()
            nm = re.match(r"\s*(\{[^}]*\}|\w+)(?:@(\(([^)]*)\)|\w+))?", text[i + 2:])
            if not nm:
                raise Unsupported("a data flow statement has no name after ~>")
            name = nm.group(1).strip("{}")
            outs = [x.strip() for x in nm.group(3).split(",")] if nm.group(3) else ([nm.group(2)] if nm.group(2) else [])
            i += 2 + nm.end()
            p = _first_paren(body)
            head = body[:p].split()
            if not head:
                raise Unsupported(f"cannot read the statement for {name}")
            kind = head[-1]
            inputs = [x.strip() for x in " ".join(head[:-1]).split(",") if x.strip()]
            args = _split_top(body[p + 1:_balanced(body, p, "(", ")") - 1])
            nodes.append(Node(name, kind.lower() if kind.lower() != "lookup" else "lookup", inputs, args, outs))
            start = i
            continue
        i += 1
    if text[start:].strip():
        raise Unsupported(f"the data flow script has text after its last statement: {text[start:start + 40]!r}")
    return params, nodes


def _first_paren(body: str) -> int:
    depth = 0
    for i, c in enumerate(body):
        if c == "(" and depth == 0:
            return i
        if c in ")]}":
            depth -= 1
        elif c in "[{":
            depth += 1
    raise Unsupported(f"no transformation call in {body[:40]!r}")


SUPPORTED = {"source", "sink", "select", "filter", "derive", "sort", "aggregate", "join", "union", "exists", "lookup", "split", "keygenerate", "alterrow", "rank", "window", "pivot", "unpivot", "folddown"}
JOIN_TYPES = {"inner": "inner", "outer": "outer", "full": "outer", "left_outer": "left_outer", "left": "left_outer", "right_outer": "right_outer",
              "right": "right_outer", "cross": "cross"}


def dfs_string(s: str) -> str | None:
    """The text of a script string literal ('...' with backslash escapes), or None when the value is not a plain literal."""
    m = re.fullmatch(r"'((?:\\.|[^'\\])*)'", s.strip(), re.S)
    return None if m is None else bytes(m.group(1), "utf-8").decode("unicode_escape")


def string_list(s: str) -> list[str] | None:
    """['a', 'b'] as a list of strings, or None when it is anything else."""
    s = s.strip()
    if not (s.startswith("[") and s.endswith("]")):
        return None
    out = [dfs_string(x) for x in _split_top(s[1:-1])]
    return None if any(x is None for x in out) else out


def _bool_opt(n: Node, key: str) -> bool:
    return n.opts.get(key, "false").strip().lower() == "true"


def check_flow(nodes: list[Node]) -> list[str]:
    """Reasons this flow cannot be translated (empty when it can). Nothing is guessed: any transformation or option outside the supported set is a reason."""
    why: list[str] = []
    for n in nodes:
        if n.kind not in SUPPORTED:
            why.append(f"transformation {n.kind!r} ({n.name}) is not translated")
            continue
        if n.outputs and n.kind != "split":
            why.append(f"{n.name} names output streams")
        try:
            plan_node(n)
        except Unsupported as e:
            why.append(str(e))
    names = {n.name for n in nodes} | {f"{n.name}@{o}" for n in nodes for o in n.outputs}
    for n in nodes:
        for i in n.inputs:
            if i not in names and i.split("@")[0] not in names:
                why.append(f"{n.name} reads {i!r}, which is not a stream of this flow")
    if not any(n.kind == "sink" for n in nodes):
        why.append("the flow has no sink")
    return why


def _unsup(msg: str):
    raise Unsupported(msg)


def _assignments(n: Node) -> list[tuple[str, str]]:
    out = []
    for a in n.pos:
        m = re.match(r"^(\{[^}]*\}|[A-Za-z_]\w*)\s*=(?!=)\s*(.*)$", a, re.S)
        if not m:
            raise Unsupported(f"{n.kind} {n.name}: cannot read the column expression {a[:40]!r} (column patterns are not translated)")
        out.append((m.group(1).strip("{}"), m.group(2).strip()))
    return out


def plan_node(n: Node) -> dict:
    """Validate a node and compile its expressions; returns what the executor needs."""
    k = n.kind
    n.pos = [p for p in n.pos if not p.startswith("partitionBy(")]      # repartitioning changes where rows are processed, not which rows there are
    if k == "source":
        if "output" in n.opts or any(p.startswith("output(") for p in n.pos):
            pass
        for opt in ("wildcardPaths", "partitionBy", "rowUrlColumn", "fileList", "fileListPath"):
            if opt in n.opts:
                raise Unsupported(f"source {n.name}: the option {opt} is not translated")
        return {"schema": source_schema(n)}
    if k == "sink":
        for opt in ("recreate", "preCommands", "postCommands", "postSQLs"):
            if opt in n.opts and n.opts[opt].strip().lower() not in ("false", "[]", "''"):
                raise Unsupported(f"sink {n.name}: {opt} is not translated")
        if any(_bool_opt(n, o) for o in ("updateable", "deletable", "upsertable")) and not string_list(n.opts.get("keys", "[]")):
            raise Unsupported(f"sink {n.name}: updates, deletes and upserts need literal key columns")
        if "insertable" in n.opts and not any(_bool_opt(n, o) for o in ("insertable", "updateable", "deletable", "upsertable")):
            raise Unsupported(f"sink {n.name}: no row policy is allowed")
        return {}
    if k == "select":
        maps = []
        for p in n.pos:
            if p.startswith("mapColumn("):
                for item in _split_top(p[len("mapColumn("):-1]):
                    m = re.match(r"^(\{[^}]*\}|[\w@]+)\s*=\s*(\{[^}]*\}|[\w@]+)$", item)
                    if m:
                        maps.append((m.group(1).strip("{}"), m.group(2).strip("{}")))
                    elif re.fullmatch(r"\{[^}]*\}|[\w@]+", item):
                        maps.append((item.strip("{}"), item.strip("{}")))
                    else:
                        raise Unsupported(f"select {n.name}: the mapping {item!r} is a column pattern, which is not translated")
            else:
                raise Unsupported(f"select {n.name}: {p[:30]!r} is not translated")
        if not maps:
            raise Unsupported(f"select {n.name}: no mapColumn list")
        return {"maps": maps}
    if k == "filter":
        if len(n.pos) != 1:
            raise Unsupported(f"filter {n.name}: expected one condition")
        return {"f": compile_expr(n.pos[0])}
    if k == "derive":
        return {"cols": [(c, compile_expr(e)) for c, e in _assignments(n)]}
    if k == "sort":
        keys = []
        for p in n.pos:
            m = re.match(r"^(asc|desc)\((.*),\s*(true|false)\)$", p, re.S | re.I)
            if not m:
                raise Unsupported(f"sort {n.name}: cannot read {p[:30]!r}")
            keys.append((compile_expr(m.group(2)), m.group(1).lower() == "desc", m.group(3).lower() == "true"))
        if not keys:
            raise Unsupported(f"sort {n.name}: no sort conditions")
        return {"keys": keys, "ci": _bool_opt(n, "caseInsensitive")}
    if k == "aggregate":
        groups, aggs = [], []
        for p in n.pos:
            if p.startswith("groupBy("):
                for g in _split_top(p[len("groupBy("):-1]):
                    m = re.match(r"^(\{[^}]*\}|[A-Za-z_]\w*)\s*=(?!=)\s*(.*)$", g, re.S)
                    groups.append((m.group(1).strip("{}"), compile_expr(m.group(2))) if m else (g.strip("{}"), compile_expr(g)))
            else:
                break
        rest = Node(n.name, k, [], [p for p in n.pos if not p.startswith("groupBy(")])
        aggs = [(c, compile_expr(e, agg=True)) for c, e in _assignments(rest)]
        return {"groups": groups, "aggs": aggs}
    if k == "join":
        jt = n.opts.get("joinType", "'inner'").strip("'")
        if jt not in JOIN_TYPES:
            raise Unsupported(f"join {n.name}: join type {jt!r} is not translated")
        if len(n.inputs) != 2:
            raise Unsupported(f"join {n.name}: expected two input streams")
        if "matchType" in n.opts and n.opts["matchType"].strip("'") != "exact":
            raise Unsupported(f"join {n.name}: fuzzy matching is not translated")
        if not n.pos:
            raise Unsupported(f"join {n.name}: no condition")
        return {"cond": compile_expr(n.pos[0]), "type": JOIN_TYPES[jt]}
    if k == "union":
        return {"by": n.opts.get("unionBy", "'name'").strip("'")}
    if k == "exists":
        if len(n.inputs) != 2 or len(n.pos) != 1:
            raise Unsupported(f"exists {n.name}: expected two input streams and one condition")
        return {"cond": compile_expr(n.pos[0]), "negate": _bool_opt(n, "negate")}
    if k == "lookup":
        if len(n.inputs) != 2 or not n.pos:
            raise Unsupported(f"lookup {n.name}: expected two input streams and a condition")
        multiple = _bool_opt(n, "multiple")
        pickup = n.opts.get("pickup", "'any'").strip("'")
        sorts = []
        for p in n.pos[1:]:
            m = re.match(r"^(asc|desc)\((.*),\s*(true|false)\)$", p, re.S | re.I)
            if not m:
                raise Unsupported(f"lookup {n.name}: cannot read {p[:30]!r}")
            sorts.append((compile_expr(m.group(2)), m.group(1).lower() == "desc", m.group(3).lower() == "true"))
        if not multiple and pickup in ("first", "last") and not sorts:
            raise Unsupported(f"lookup {n.name}: pickup {pickup!r} needs sort conditions")
        return {"cond": compile_expr(n.pos[0]), "multiple": multiple, "pickup": pickup, "sorts": sorts}
    if k == "split":
        conds = [compile_expr(p) for p in n.pos]
        if not conds or len(n.outputs) not in (len(conds), len(conds) + 1):
            raise Unsupported(f"split {n.name}: the output streams do not match the conditions")
        return {"conds": conds, "disjoint": _bool_opt(n, "disjoint")}
    if k == "keygenerate":
        out = [p for p in n.pos if p.startswith("output(")]
        m = re.fullmatch(r"output\(\s*(\{[^}]*\}|\w+)\s+as\s+long\s*\)", out[0], re.S) if out else None
        if not m or len(out) != 1:
            raise Unsupported(f"keyGenerate {n.name}: expected output(<column> as long)")
        start = re.fullmatch(r"(-?\d+)L?", n.opts.get("startAt", "1L").strip())
        if not start or not re.fullmatch(r"1L?", n.opts.get("stepValue", "1L").strip()):
            raise Unsupported(f"keyGenerate {n.name}: startAt must be a number and stepValue is not translated")
        return {"col": m.group(1).strip("{}"), "start": int(start.group(1))}
    if k == "window":
        over, keys, cols = [], [], []
        for p in n.pos:
            m = re.match(r"^(asc|desc)\((.*),\s*(true|false)\)$", p, re.S | re.I)
            if p.startswith("over(") and p.endswith(")"):
                over = [compile_expr(x) for x in _split_top(p[5:-1])]
            elif m:
                keys.append((compile_expr(m.group(2)), m.group(1).lower() == "desc", m.group(3).lower() == "true"))
            else:
                cols.append(p)
        for opt in ("startRowOffset", "endRowOffset", "startRangeOffset", "endRangeOffset"):
            if opt in n.opts:
                raise Unsupported(f"window {n.name}: a bounded frame ({opt}) is not translated; only the whole partition is")
        tmp = Node(n.name, k, [], cols)
        return {"over": over, "keys": keys, "cols": [(c, compile_expr(e, win=True)) for c, e in _assignments(tmp)]}
    if k == "pivot":
        groups, by, vals, cols = [], None, None, []
        for p in n.pos:
            if p.startswith("groupBy(") and p.endswith(")"):
                groups = [x.strip("{}") if re.fullmatch(r"\{[^}]*\}|\w+", x) else _unsup(f"pivot {n.name}: groupBy needs plain columns") for x in _split_top(p[8:-1])]
            elif p.startswith("pivotBy(") and p.endswith(")"):
                parts = _split_top(p[8:-1])
                if not parts or not re.fullmatch(r"\{[^}]*\}|\w+", parts[0]):
                    raise Unsupported(f"pivot {n.name}: pivotBy needs a plain column")
                by = parts[0].strip("{}")
                if len(parts) > 1:
                    vals = string_list(parts[1])
                    if vals is None:
                        raise Unsupported(f"pivot {n.name}: the pivot values are not a list of plain strings")
            else:
                cols.append(p)
        if by is None:
            raise Unsupported(f"pivot {n.name}: no pivotBy")
        naming = dfs_string(n.opts.get("columnNaming", "''"))
        if not naming:
            raise Unsupported(f"pivot {n.name}: no columnNaming pattern (its default is not documented)")
        pcols = []
        for item in cols:
            m = re.match(r"^(\{[^}]*\}|\w*)\s*=(?!=)\s*(.*)$", item, re.S)
            if not m:
                raise Unsupported(f"pivot {n.name}: cannot read the pivoted column {item[:40]!r}")
            pcols.append((m.group(1).strip("{}"), compile_expr(m.group(2), agg=True)))
        if not pcols:
            raise Unsupported(f"pivot {n.name}: no pivoted columns")
        return {"groups": groups, "by": by, "values": vals, "cols": pcols, "naming": naming}
    if k == "unpivot":
        out = [p for p in n.pos if p.startswith("output(")]
        ung = [p for p in n.pos if p.startswith("ungroupBy(")]
        if len(out) != 1 or len(ung) != 1:
            raise Unsupported(f"unpivot {n.name}: expected output(...) and ungroupBy(...)")
        cols = []
        for item in _split_top(out[0][7:-1]):
            m = re.match(r"^(\{[^}]*\}|\w+)\s+as\s+(.+)$", item, re.S)
            if not m:
                raise Unsupported(f"unpivot {n.name}: cannot read {item!r}")
            cols.append((m.group(1).strip("{}"), m.group(2).strip()))
        if len(cols) != 2:
            raise Unsupported(f"unpivot {n.name}: expected a key column and one value column")
        groups = [x.strip("{}") for x in _split_top(ung[0][10:-1])]
        values = string_list(n.opts.get("pivotValues", "[]")) if "pivotValues" in n.opts else None
        if "pivotValues" in n.opts and values is None:
            raise Unsupported(f"unpivot {n.name}: pivotValues is not a list of plain strings")
        return {"key": cols[0], "value": cols[1], "groups": groups, "values": values, "drop_null": _bool_opt(n, "ignoreNullPivots")}
    if k == "folddown":
        unroll, maps = None, []
        for p in n.pos:
            if p.startswith("unroll(") and p.endswith(")"):
                unroll = _split_top(p[7:-1])
            elif p.startswith("mapColumn(") and p.endswith(")"):
                for item in _split_top(p[10:-1]):
                    m = re.match(r"^(\{[^}]*\}|\w+)\s*=\s*([\w.{} ]+)$", item)
                    if m:
                        maps.append((m.group(1).strip("{}"), m.group(2).strip()))
                    elif re.fullmatch(r"\{[^}]*\}|[\w.]+", item):
                        maps.append((item.strip("{}").split(".")[-1], item.strip("{}")))
                    else:
                        raise Unsupported(f"flatten {n.name}: the mapping {item[:40]!r} is a rule-based mapping, which is not translated")
            else:
                raise Unsupported(f"flatten {n.name}: cannot read {p[:30]!r}")
        if not unroll or len(unroll) > 2 or not all(re.fullmatch(r"[\w.]+", u) for u in unroll) or not maps:
            raise Unsupported(f"flatten {n.name}: expected unroll(<array>[, <root>]) with plain paths and a mapColumn list")
        by_path, root = unroll[0], unroll[-1]
        if len(unroll) == 2 and not (by_path == root or by_path.startswith(root + ".")):
            raise Unsupported(f"flatten {n.name}: the unroll root {root} does not contain the array {by_path}")
        return {"by": by_path, "root": root if len(unroll) == 2 else None, "maps": maps}
    if k == "rank":
        keys = []
        out = None
        for p in n.pos:
            m = re.match(r"^(asc|desc)\((.*),\s*(true|false)\)$", p, re.S | re.I)
            o = re.fullmatch(r"output\(\s*(\{[^}]*\}|\w+)\s+as\s+long\s*\)", p, re.S)
            if m:
                keys.append((compile_expr(m.group(2)), m.group(1).lower() == "desc", m.group(3).lower() == "true"))
            elif o:
                out = o.group(1).strip("{}")
            else:
                raise Unsupported(f"rank {n.name}: cannot read {p[:30]!r}")
        if not keys or out is None:
            raise Unsupported(f"rank {n.name}: needs sort conditions (with the null-ordering flag) and output(<column> as long)")
        return {"keys": keys, "ci": _bool_opt(n, "caseInsensitive"), "dense": _bool_opt(n, "dense"), "col": out}
    if k == "alterrow":
        pols = []
        for p in n.pos:
            m = re.match(r"^(insert|update|delete|upsert)If\((.*)\)$", p, re.S | re.I)
            if not m:
                raise Unsupported(f"alterRow {n.name}: cannot read {p[:30]!r}")
            pols.append((m.group(1).lower(), compile_expr(m.group(2).strip() or "true()")))
        return {"policies": pols}
    raise Unsupported(f"transformation {k!r} is not translated")


def source_schema(n: Node) -> list[tuple[str, str]]:
    """The declared output columns of a source: [(name, type)], from `output(col as type, ...)`."""
    for p in n.pos:
        if p.startswith("output("):
            cols = []
            for item in _split_top(p[len("output("):-1]):
                m = re.match(r"^(\{[^}]*\}|[\w.]+)\s+as\s+(.+)$", item, re.S)
                if not m:
                    raise Unsupported(f"source {n.name}: cannot read the column {item!r}")
                cols.append((m.group(1).strip("{}"), m.group(2).strip()))
            return cols
    return []


_CASTS = {"string": lambda v: str(v), "integer": lambda v: D.f_tointeger(v), "long": lambda v: D.f_tolong(v), "short": lambda v: D.f_toshort(v),
          "double": lambda v: D.f_todouble(v), "float": lambda v: D.f_tofloat(v), "boolean": lambda v: D.f_toboolean(v),
          "date": lambda v: D.f_todate(v), "timestamp": lambda v: D.f_totimestamp(v)}


def cast_value(v, typ: str):
    """A text value as the declared type (delimited text arrives as text); null stays null; a value that does not convert becomes null, as in data flows."""
    if v is None or v == "":
        return None
    t = typ.strip().lower()
    m = re.match(r"decimal\((\d+),\s*(\d+)\)", t)
    try:
        if m:
            return D.f_todecimal(v, int(m.group(1)), int(m.group(2)))
        f = _CASTS.get(t)
        if f is None:
            raise Unsupported(f"the column type {typ!r} is not translated")
        return f(v)
    except (ValueError, TypeError, ArithmeticError, _dec.InvalidOperation):
        return None


# ---------------------------------------------------------------------------------------------------------------------------------------------
# executor: every stream is a list of dicts. `io` supplies the sources and takes the sinks.
# ---------------------------------------------------------------------------------------------------------------------------------------------

def _sort_key(v, desc: bool, nulls_first: bool, ci: bool):
    if v is None:
        return (0 if nulls_first else 2, 0)
    if ci and isinstance(v, str):
        v = v.lower()
    return (1, v)


def run_flow(script: str, io, params: dict | None = None) -> dict[str, int]:
    """Run a data flow script. `io.read(source_name, schema)` returns a list of dicts and `io.write(sink_name, rows, opts)` stores one; the result is the rows each
    sink wrote. Parameters not passed take their declared default."""
    declared, nodes = parse_script(script)
    why = check_flow(nodes)
    if why:
        raise Unsupported("; ".join(why))
    P: dict[str, Any] = {}
    for name, (typ, default) in declared.items():
        if params and name in params:
            P[name] = params[name]
        elif default is not None:
            P[name] = compile_expr(default)({}, P)
        else:
            P[name] = None
    for name, v in (params or {}).items():
        P.setdefault(name, v)
    streams: dict[str, list[dict]] = {}
    written: dict[str, int] = {}
    pending = list(nodes)
    while pending:
        progressed = False
        for n in list(pending):
            keys = [i if i in streams else i.split("@")[0] for i in n.inputs]
            ins = [streams[k] for k in keys if k in streams]
            if len(ins) != len(n.inputs):
                continue
            pending.remove(n)
            progressed = True
            plan = plan_node(n)
            if n.kind == "source":
                rows = io.read(n.name, plan["schema"])
                streams[n.name] = [dict(r) for r in rows]
            elif n.kind == "sink":
                written[n.name] = io.write(n.name, ins[0], {k: v for k, v in n.opts.items()})
            elif n.kind == "split":
                for out, rows_out in zip(n.outputs, _split(plan, ins[0], P, len(n.outputs))):
                    streams[f"{n.name}@{out}"] = rows_out
            else:
                streams[n.name] = _apply(n, plan, ins, P)
        if not progressed:
            raise Unsupported("the data flow has a cycle or an unknown input stream")
    return written


def _apply(n: Node, plan: dict, ins: list[list[dict]], P: dict) -> list[dict]:
    k = n.kind
    rows = ins[0]
    if k == "filter":
        return [r for r in rows if plan["f"](r, P) is True]
    if k == "derive":
        out = []
        for r in rows:
            new = dict(r)
            for col, f in plan["cols"]:
                new[col] = f(r, P)          # an exact name replaces the column; a name that differs only in case is a new column (a real flow derives PAIS beside Pais)
                # every expression sees the incoming row, not the columns derived before it in this transformation
            out.append(new)
        return out
    if k == "select":
        out = []
        for r in rows:
            out.append({new: D.col(r, old.split("@", 1)[1], old.split("@", 1)[0]) if "@" in old else D.col(r, old) for new, old in plan["maps"]})
        return out
    if k == "sort":
        out = list(rows)
        for f, desc, nulls_first in reversed(plan["keys"]):           # stable sorts from the last key to the first
            nonnull = [r for r in out if f(r, P) is not None]
            nulls = [r for r in out if f(r, P) is None]
            nonnull.sort(key=lambda r: _sort_key(f(r, P), desc, nulls_first, plan["ci"]), reverse=desc)
            out = nulls + nonnull if nulls_first else nonnull + nulls
        return out
    if k == "aggregate":
        groups: dict[tuple, list[dict]] = {}
        for r in rows:
            groups.setdefault(tuple(f(r, P) for _, f in plan["groups"]), []).append(r)
        if not plan["groups"] and not groups:
            groups[()] = []
        out = []
        for key, grp in groups.items():
            rec = {name: v for (name, _), v in zip(plan["groups"], key)}
            for col, f in plan["aggs"]:
                rec[col] = f(grp[0] if grp else {}, P, grp)
            out.append(rec)
        return out
    if k == "union":
        left, right = ins[0], ins[1]
        if plan["by"] == "name":
            cols = list(dict.fromkeys([*(left[0] if left else {}), *(right[0] if right else {})]))
            return [{c: D.col_or_none(r, c) for c in cols} for r in [*left, *right]]
        lc = list(left[0]) if left else []
        rc = list(right[0]) if right else []
        return [dict(r) for r in left] + [{lc[i] if i < len(lc) else rc[i]: r[rc[i]] for i in range(len(rc))} for r in right]
    if k == "join":
        return _join(n, plan, ins, P)
    if k == "exists":
        return _exists(n, plan, ins, P)
    if k == "lookup":
        return _lookup(n, plan, ins, P)
    if k == "window":
        return _window(plan, rows, P)
    if k == "pivot":
        return _pivot(plan, rows, P)
    if k == "unpivot":
        return _unpivot(plan, rows)
    if k == "folddown":
        return _flatten(plan, rows)
    if k == "rank":
        def key_of(r):
            return tuple(_sort_key(f(r, P), d, nf, plan["ci"]) for f, d, nf in plan["keys"])
        # order the rows by the sort conditions, number the ties together, and hand the rows back in their incoming order
        ordered = list(rows)
        for f, desc, nulls_first in reversed(plan["keys"]):
            nn = sorted((r for r in ordered if f(r, P) is not None), key=lambda r: _sort_key(f(r, P), desc, nulls_first, plan["ci"]), reverse=desc)
            nu = [r for r in ordered if f(r, P) is None]
            ordered = nu + nn if nulls_first else nn + nu
        ranks: dict[int, int] = {}
        prev, rank, dense = None, 0, 0
        for pos, r in enumerate(ordered, 1):
            kk = key_of(r)
            if kk != prev:
                rank, dense, prev = pos, dense + 1, kk
            ranks[id(r)] = dense if plan["dense"] else rank
        return [{**r, plan["col"]: ranks[id(r)]} for r in rows]
    if k == "keygenerate":
        return [{**r, plan["col"]: plan["start"] + i} for i, r in enumerate(rows)]
    if k == "alterrow":
        out = []
        for r in rows:
            policy = next((name for name, f in plan["policies"] if f(r, P) is True), "insert")       # a row that matches no policy is inserted
            out.append({**r, "__alterRow__": policy})
        return out
    raise Unsupported(f"transformation {k!r} is not translated")


def _split(plan: dict, rows: list[dict], P: dict, n_out: int) -> list[list[dict]]:
    """Conditional split: with `disjoint: false` a row goes to the first stream whose condition is true; with `disjoint: true`, to every stream whose condition is
    true. Rows that match nothing go to the default stream, the last output when there is one more output than conditions."""
    n_cond = len(plan["conds"])
    outs: list[list[dict]] = [[] for _ in range(n_out)]
    for r in rows:
        hit = False
        for i, f in enumerate(plan["conds"]):
            if f(r, P) is True:
                outs[i].append(dict(r))
                hit = True
                if not plan["disjoint"]:
                    break
        if not hit and n_out > n_cond:
            outs[n_cond].append(dict(r))
    return outs


def _pairs(n: Node, ins: list[list[dict]]):
    """For two input streams: (left stream name, right stream name, combine(a, b), test(cond, a, b, P)). A combined row has every column of both streams as
    `Stream@column`, plus the plain name for the left stream's columns and for the right stream's columns that the left stream lacks; a plain name that both
    streams have is ambiguous in a condition and raises, as in ADF."""
    ln, rn = (i.split("@")[0] for i in n.inputs)
    left, right = ins
    lcols = list(left[0]) if left else []
    rcols = list(right[0]) if right else []
    both = {c.lower() for c in lcols} & {c.lower() for c in rcols}

    def combine(a, b):
        row: dict = {}
        for c in lcols:
            row[f"{ln}@{c}"] = a[c] if a else None
        for c in rcols:
            row[f"{rn}@{c}"] = b[c] if b else None
        for c in lcols:
            row[c] = a[c] if a else None
        for c in rcols:
            if c.lower() not in both:
                row[c] = b[c] if b else None
        return row

    def test(cond, a, b, P) -> bool:
        probe = {}
        for c in lcols:
            probe[f"{ln}@{c}"] = a[c]
            if c.lower() not in both:
                probe[c] = a[c]
        for c in rcols:
            probe[f"{rn}@{c}"] = b[c]
            if c.lower() not in both:
                probe[c] = b[c]
        return cond(probe, P) is True

    return combine, test


def _join(n: Node, plan: dict, ins: list[list[dict]], P: dict) -> list[dict]:
    left, right = ins
    combine, test = _pairs(n, ins)
    out, matched_right = [], set()
    for a in left:
        hit = False
        for j, b in enumerate(right):
            if test(plan["cond"], a, b, P):
                out.append(combine(a, b))
                matched_right.add(j)
                hit = True
        if not hit and plan["type"] in ("outer", "left_outer"):
            out.append(combine(a, None))
    if plan["type"] in ("outer", "right_outer"):
        for j, b in enumerate(right):
            if j not in matched_right:
                out.append(combine(None, b))
    return out


def _exists(n: Node, plan: dict, ins: list[list[dict]], P: dict) -> list[dict]:
    """Keep the left stream's rows for which some right row satisfies the condition (or, with negate, none does). Only the left stream's columns come out."""
    left, right = ins
    _, test = _pairs(n, ins)
    return [dict(a) for a in left if any(test(plan["cond"], a, b, P) for b in right) != plan["negate"]]


def _lookup(n: Node, plan: dict, ins: list[list[dict]], P: dict) -> list[dict]:
    """Every primary row, with the columns of its matching lookup row (null when none matches; isMatch() says which). Without 'multiple', one match is picked:
    the first or last by the sort conditions, or, for 'any', the first found."""
    left, right = ins
    combine, test = _pairs(n, ins)
    out = []
    for a in left:
        hits = [b for b in right if test(plan["cond"], a, b, P)]
        if hits and not plan["multiple"]:
            if plan["pickup"] in ("first", "last"):
                for f, desc, nulls_first in reversed(plan["sorts"]):
                    nn = sorted((b for b in hits if f(b, P) is not None), key=lambda b: _sort_key(f(b, P), desc, nulls_first, False), reverse=desc)
                    nu = [b for b in hits if f(b, P) is None]
                    hits = nu + nn if nulls_first else nn + nu
                hits = [hits[0] if plan["pickup"] == "first" else hits[-1]]
            else:
                hits = hits[:1]
        for b in hits or [None]:
            out.append({**combine(a, b), "__isMatch__": b is not None})
    return out


def _order(rows: list[dict], keys: list, P: dict, ci: bool = False) -> list[dict]:
    out = list(rows)
    for f, desc, nulls_first in reversed(keys):
        nn = sorted((r for r in out if f(r, P) is not None), key=lambda r: _sort_key(f(r, P), desc, nulls_first, ci), reverse=desc)
        nu = [r for r in out if f(r, P) is None]
        out = nu + nn if nulls_first else nn + nu
    return out


def _window(plan: dict, rows: list[dict], P: dict) -> list[dict]:
    """Partition the rows by the over(...) expressions, order each partition by the sort conditions, and evaluate each window column over the whole partition (the
    analytic functions see their row's place in the ordering). The rows come back in their incoming order."""
    parts: dict[tuple, list[dict]] = {}
    for r in rows:
        parts.setdefault(tuple(f(r, P) for f in plan["over"]), []).append(r)
    extra: dict[int, dict] = {}
    for part in parts.values():
        ordered = _order(part, plan["keys"], P)
        keys = [tuple(_sort_key(f(r, P), d, nf, False) for f, d, nf in plan["keys"]) for r in ordered]
        for col, f in plan["cols"]:
            whole = None if f.analytic else f(ordered[0], P, ordered, None)
            for i, r in enumerate(ordered):
                extra.setdefault(id(r), {})[col] = f(r, P, ordered, _Win(ordered, i, keys)) if f.analytic else whole
    return [{**r, **extra[id(r)]} for r in rows]


def _pivot(plan: dict, rows: list[dict], P: dict) -> list[dict]:
    """One row per group; for each pivot value and pivoted column a column named by columnNaming ($N is the pivoted column's name, $V the value), holding the
    aggregate over that group's rows with that value (null when there are none). Without listed values, every distinct value in sorted order."""
    by = plan["by"]
    values = plan["values"] if plan["values"] is not None else sorted({str(D.col(r, by)) for r in rows if D.col(r, by) is not None})
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault(tuple(D.col(r, g) for g in plan["groups"]), []).append(r)
    if not plan["groups"] and not groups:
        groups[()] = []
    out = []
    for key, grp in groups.items():
        rec = dict(zip(plan["groups"], key))
        for v in values:
            sub = [r for r in grp if str(D.col(r, by)) == v and D.col(r, by) is not None]
            for name, f in plan["cols"]:
                rec[plan["naming"].replace("$N", name).replace("$V", v)] = f(sub[0], P, sub) if sub else None
        out.append(rec)
    return out


def _unpivot(plan: dict, rows: list[dict]) -> list[dict]:
    """Each row becomes one row per unpivoted column: the ungrouped columns, the column's name in the key column and its value in the value column. Without
    pivotValues every column that is not ungrouped is unpivoted; ignoreNullPivots drops null values."""
    (kname, _), (vname, vtype) = plan["key"], plan["value"]
    out = []
    for r in rows:
        cols = plan["values"] if plan["values"] is not None else [c for c in r if c.lower() not in {g.lower() for g in plan["groups"]}]
        for c in cols:
            v = D.col(r, c)
            if isinstance(v, str):
                try:
                    v = cast_value(v, vtype)
                except Unsupported:
                    pass
            if v is None and plan["drop_null"]:
                continue
            out.append({**{g: D.col(r, g) for g in plan["groups"]}, kname: c, vname: v})
    return out


def _path(obj, path: str):
    for part in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part, next((v for k, v in obj.items() if k.lower() == part.lower()), None))
        else:
            return None
    return obj


def _flatten(plan: dict, rows: list[dict]) -> list[dict]:
    """Unroll an array of nested JSON: one output row per array item. Without an unroll root, a null or empty array gives one row with null unrolled values; with a
    root (an array of objects that is or contains the array), a row with no root items is dropped. Mapped paths below the array read the current item."""
    by, root = plan["by"], plan["root"]
    out = []
    for r in rows:
        if root is None:
            items = _path(r, by)
            items = items if isinstance(items, list) and items else [None]
            frames = [(None, it) for it in items]
        else:
            roots = _path(r, root)
            roots = roots if isinstance(roots, list) else []
            frames = []
            for ro in roots:
                if by == root:
                    frames.append((ro, ro))
                    continue
                sub = _path(ro, by[len(root) + 1:])
                for it in (sub if isinstance(sub, list) and sub else [None]):
                    frames.append((ro, it))
        for ro, it in frames:
            rec = {}
            for name, path in plan["maps"]:
                if path == by or path.startswith(by + "."):
                    rec[name] = it if path == by else _path(it, path[len(by) + 1:])
                elif root is not None and (path == root or path.startswith(root + ".")):
                    rec[name] = ro if path == root else _path(ro, path[len(root) + 1:])
                else:
                    rec[name] = _path(r, path)
            out.append(rec)
    return out
