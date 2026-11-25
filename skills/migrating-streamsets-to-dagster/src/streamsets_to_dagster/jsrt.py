"""JavaScript semantics for scripts transpiled to Python (copied into generated projects as `jsrt.py`).

The transpiler (`jstopy.py`) maps JavaScript operators and property access onto these helpers so that a script keeps its JavaScript meaning
(truthiness, `+` on strings, `==` versus `===`, `undefined` versus `null`, string and array methods) while the generated code is plain Python.
"""
from __future__ import annotations

import functools
import json
import math
import re


class _Undef:
    _inst = None

    def __new__(cls):
        if cls._inst is None:
            cls._inst = super().__new__(cls)
        return cls._inst

    def __bool__(self):
        return False

    def __repr__(self):
        return "undefined"


UNDEF = _Undef()


class JSError(Exception):
    """A thrown JavaScript value."""

    def __init__(self, value):
        super().__init__(tostr(value))
        self.value = value


class JSTypeError(JSError):
    def __init__(self, msg):
        super().__init__(ErrorObj("TypeError", msg))


class ErrorObj(dict):
    def __init__(self, name, message=""):
        super().__init__(name=name, message=message)

    def __str__(self):
        return f"{self['name']}: {self['message']}" if self["message"] else self["name"]


class JSRecord:
    """What scripts see: `.value` (a map), `.attributes`, `.sourceId`."""

    def __init__(self, value=None, attributes=None, sourceId="", eventType=None):
        self.value, self.attributes, self.sourceId, self.eventType = value, (attributes if attributes is not None else {}), sourceId, eventType


class JSRegex:
    def __init__(self, pattern, flags=""):
        self.source, self.flags = pattern, flags
        f = (re.I if "i" in flags else 0) | (re.M if "m" in flags else 0) | (re.S if "s" in flags else 0)
        self.rx = re.compile(pattern, f)
        self.glob = "g" in flags

    def test(self, s):
        return self.rx.search(tostr(s)) is not None


# ---- conversions ---------------------------------------------------------------------------------------------------------------------------
def truthy(x) -> bool:
    if x is None or x is UNDEF or x is False:
        return False
    if isinstance(x, (int, float)):
        return not (x == 0 or (isinstance(x, float) and math.isnan(x)))
    if isinstance(x, str):
        return x != ""
    return True                                   # objects and arrays are always truthy, even when empty


def typeof(x) -> str:
    if x is UNDEF:
        return "undefined"
    if x is None:
        return "object"
    if isinstance(x, bool):
        return "boolean"
    if isinstance(x, (int, float)):
        return "number"
    if isinstance(x, str):
        return "string"
    return "function" if callable(x) and not isinstance(x, (dict, list, JSRegex)) else "object"


def _numstr(x) -> str:
    if isinstance(x, bool):
        return "true" if x else "false"
    if isinstance(x, int):
        return str(x)
    if math.isnan(x):
        return "NaN"
    if math.isinf(x):
        return "Infinity" if x > 0 else "-Infinity"
    if x == int(x) and abs(x) < 1e21:
        return str(int(x))
    r = repr(x)
    return r.replace("e+", "e+") if "e" in r else r


def tostr(x) -> str:
    if x is UNDEF:
        return "undefined"
    if x is None:
        return "null"
    if isinstance(x, (bool, int, float)):
        return _numstr(x)
    if isinstance(x, str):
        return x
    if isinstance(x, list):
        return ",".join("" if (e is None or e is UNDEF) else tostr(e) for e in x)
    if isinstance(x, ErrorObj):
        return str(x)
    if isinstance(x, dict):
        return "[object Object]"
    return str(x)


def num(x):
    if isinstance(x, bool):
        return int(x)
    if isinstance(x, (int, float)):
        return x
    if x is None:
        return 0
    if x is UNDEF:
        return float("nan")
    if isinstance(x, str):
        s = x.strip()
        if s == "":
            return 0
        try:
            return int(s) if re.fullmatch(r"[+-]?\d+", s) else float(s)
        except ValueError:
            return float("nan")
    if isinstance(x, list):
        return num(tostr(x))
    return float("nan")


def _prim(x):
    return tostr(x) if isinstance(x, (list, dict)) else x


def add(a, b):
    a, b = _prim(a), _prim(b)
    if isinstance(a, str) or isinstance(b, str):
        return tostr(a) + tostr(b)
    return num(a) + num(b)


def sub(a, b):
    return num(a) - num(b)


def mul(a, b):
    return num(a) * num(b)


def div(a, b):
    a, b = num(a), num(b)
    if b == 0:
        return float("nan") if (a == 0 or (isinstance(a, float) and math.isnan(a))) else math.copysign(float("inf"), a) * (1 if str(b)[0] != "-" else -1)
    r = a / b
    return int(r) if isinstance(r, float) and r == int(r) and abs(r) < 1e15 else r


def mod(a, b):
    a, b = num(a), num(b)
    if b == 0 or math.isinf(a) or math.isnan(a) or math.isnan(b):
        return float("nan")
    r = math.fmod(a, b)
    return int(r) if r == int(r) and isinstance(a, int) and isinstance(b, int) else r


def neg(a):
    return -num(a)


def eq(a, b) -> bool:
    if (a is None or a is UNDEF) and (b is None or b is UNDEF):
        return True
    if a is None or a is UNDEF or b is None or b is UNDEF:
        return False
    if isinstance(a, (dict, list, JSRecord)) and isinstance(b, (dict, list, JSRecord)):
        return a is b
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    a, b = _prim(a), _prim(b)
    if isinstance(a, str) or isinstance(b, str) or isinstance(a, bool) or isinstance(b, bool):
        return num(a) == num(b) if not (isinstance(a, str) and isinstance(b, str)) else a == b
    return a == b


def seq(a, b) -> bool:
    if a is UNDEF or b is UNDEF:
        return a is b
    if a is None or b is None:
        return a is b
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    return a is b


def _cmp(a, b, op):
    a, b = _prim(a), _prim(b)
    if isinstance(a, str) and isinstance(b, str):
        return op(a, b)
    a, b = num(a), num(b)
    if (isinstance(a, float) and math.isnan(a)) or (isinstance(b, float) and math.isnan(b)):
        return False
    return op(a, b)


def lt(a, b):
    return _cmp(a, b, lambda x, y: x < y)


def gt(a, b):
    return _cmp(a, b, lambda x, y: x > y)


def le(a, b):
    return _cmp(a, b, lambda x, y: x <= y)


def ge(a, b):
    return _cmp(a, b, lambda x, y: x >= y)


def has(o, k) -> bool:
    if isinstance(o, dict):
        return tostr(k) in o
    if isinstance(o, list):
        return isinstance(num(k), (int, float)) and 0 <= num(k) < len(o)
    raise JSTypeError("cannot use 'in' operator on a non-object")


def instanceof(x, cls) -> bool:
    return {"String": isinstance(x, str), "Number": isinstance(x, (int, float)) and not isinstance(x, bool), "Boolean": isinstance(x, bool),
            "Array": isinstance(x, list), "Object": isinstance(x, (dict, list)), "Error": isinstance(x, ErrorObj), "RegExp": isinstance(x, JSRegex)}.get(cls, False) \
        and not (cls in ("String", "Number", "Boolean") and False)


# ---- property access ---------------------------------------------------------------------------------------------------------------------
def get(o, k):
    if o is None or o is UNDEF:
        raise JSTypeError(f"cannot read property '{tostr(k)}' of {'null' if o is None else 'undefined'}")
    if isinstance(o, dict):
        return o.get(tostr(k), UNDEF)
    if isinstance(o, list):
        if k == "length":
            return len(o)
        i = num(k)
        return o[int(i)] if isinstance(i, (int, float)) and i == i and i == int(i) and 0 <= i < len(o) else UNDEF
    if isinstance(o, str):
        if k == "length":
            return len(o)
        i = num(k)
        return o[int(i)] if isinstance(i, (int, float)) and i == i and i == int(i) and 0 <= i < len(o) else UNDEF
    if isinstance(o, JSRecord):
        return getattr(o, k, UNDEF) if isinstance(k, str) else UNDEF
    if isinstance(o, JSRegex):
        return {"source": o.source, "global": o.glob, "flags": o.flags}.get(k, UNDEF)
    return getattr(o, k, UNDEF) if isinstance(k, str) else UNDEF            # the scripting objects (sdc.userParams, sdc.log, ...)


def put(o, k, v):
    if o is None or o is UNDEF:
        raise JSTypeError(f"cannot set property '{tostr(k)}' of {'null' if o is None else 'undefined'}")
    if isinstance(o, dict):
        o[tostr(k)] = v
    elif isinstance(o, list):
        i = int(num(k))
        while len(o) <= i:
            o.append(UNDEF)
        o[i] = v
    elif isinstance(o, JSRecord):
        setattr(o, k, v)
    return v


def delete(o, k):
    if isinstance(o, dict):
        o.pop(tostr(k), None)
    return True


def keys(o):
    if isinstance(o, dict):
        return list(o.keys())
    if isinstance(o, list):
        return [str(i) for i in range(len(o))]
    return []


# ---- method calls ------------------------------------------------------------------------------------------------------------------------
def _fn(f):
    if not callable(f):
        raise JSTypeError("callback is not a function")
    return f


def _clamp(i, n):
    i = int(num(i)) if num(i) == num(i) else 0
    return max(0, min(n, i))


def _slice_idx(i, n, default):
    if i is UNDEF:
        return default
    i = num(i)
    i = int(i) if i == i else 0
    return max(0, n + i) if i < 0 else min(i, n)


def _repl(rep: str):
    return re.sub(r"\$(\d)", r"\\\1", rep.replace("\\", "\\\\")) if "$" in rep else rep.replace("\\", "\\\\")


def _string_method(s: str, name, a):
    if name == "split":
        sep = a[0] if a else UNDEF
        lim = None if len(a) < 2 or a[1] is UNDEF else int(num(a[1]))
        if sep is UNDEF:
            parts = [s]
        elif isinstance(sep, JSRegex):
            parts = sep.rx.split(s)
        elif sep == "":
            parts = list(s)
        else:
            parts = s.split(tostr(sep))
        return parts[:lim] if lim is not None else parts
    if name == "replace":
        pat, rep = a[0], a[1]
        if isinstance(pat, JSRegex):
            fn = (lambda m: tostr(rep(m.group(0), *[g if g is not None else UNDEF for g in m.groups()]))) if callable(rep) else _repl(tostr(rep))
            return pat.rx.sub(fn, s, count=0 if pat.glob else 1)
        p = tostr(pat)
        return s.replace(p, tostr(rep(p)) if callable(rep) else tostr(rep), 1)
    if name == "trim":
        return s.strip()
    if name == "toLowerCase":
        return s.lower()
    if name == "toUpperCase":
        return s.upper()
    if name == "indexOf":
        return s.find(tostr(a[0]), _clamp(a[1], len(s)) if len(a) > 1 else 0)
    if name == "lastIndexOf":
        return s.rfind(tostr(a[0]))
    if name == "includes":
        return tostr(a[0]) in s
    if name == "startsWith":
        return s.startswith(tostr(a[0]))
    if name == "endsWith":
        return s.endswith(tostr(a[0]))
    if name == "charAt":
        i = _clamp(a[0] if a else 0, len(s) + 1)
        return s[i] if i < len(s) else ""
    if name == "substring":
        x, y = _clamp(a[0], len(s)), (_clamp(a[1], len(s)) if len(a) > 1 and a[1] is not UNDEF else len(s))
        return s[min(x, y):max(x, y)]
    if name == "substr":
        x = _slice_idx(a[0], len(s), 0)
        return s[x:x + int(num(a[1]))] if len(a) > 1 and a[1] is not UNDEF else s[x:]
    if name == "slice":
        return s[_slice_idx(a[0] if a else UNDEF, len(s), 0):_slice_idx(a[1] if len(a) > 1 else UNDEF, len(s), len(s))]
    if name == "concat":
        return s + "".join(tostr(x) for x in a)
    if name == "toString":
        return s
    if name == "match":
        rx = a[0] if isinstance(a[0], JSRegex) else JSRegex(tostr(a[0]))
        if rx.glob:
            ms = [m.group(0) for m in rx.rx.finditer(s)]
            return ms or None
        m = rx.rx.search(s)
        return None if not m else [m.group(0)] + [g if g is not None else UNDEF for g in m.groups()]
    if name == "localeCompare":
        o = tostr(a[0])
        return -1 if s < o else (1 if s > o else 0)
    raise JSTypeError(f"{name} is not a function")


def _array_method(l: list, name, a):
    if name == "push":
        l.extend(a)
        return len(l)
    if name == "pop":
        return l.pop() if l else UNDEF
    if name == "shift":
        return l.pop(0) if l else UNDEF
    if name == "unshift":
        l[0:0] = a
        return len(l)
    if name == "join":
        sep = "," if not a or a[0] is UNDEF else tostr(a[0])
        return sep.join("" if (e is None or e is UNDEF) else tostr(e) for e in l)
    if name == "indexOf":
        return next((i for i, e in enumerate(l) if seq(e, a[0])), -1)
    if name == "includes":
        return any(seq(e, a[0]) or (e != e and a[0] != a[0]) for e in l)
    if name == "slice":
        return l[_slice_idx(a[0] if a else UNDEF, len(l), 0):_slice_idx(a[1] if len(a) > 1 else UNDEF, len(l), len(l))]
    if name == "concat":
        out = list(l)
        for x in a:
            out.extend(x) if isinstance(x, list) else out.append(x)
        return out
    if name == "reverse":
        l.reverse()
        return l
    if name == "sort":
        if a and callable(a[0]):
            l.sort(key=functools.cmp_to_key(lambda x, y: int(num(a[0](x, y)))))
        else:
            l.sort(key=lambda x: tostr(x))
        return l
    if name == "forEach":
        f = _fn(a[0])
        for i, e in enumerate(list(l)):
            f(e, i, l)
        return UNDEF
    if name == "map":
        f = _fn(a[0])
        return [f(e, i, l) for i, e in enumerate(l)]
    if name == "filter":
        f = _fn(a[0])
        return [e for i, e in enumerate(list(l)) if truthy(f(e, i, l))]
    if name == "some":
        return any(truthy(_fn(a[0])(e, i, l)) for i, e in enumerate(l))
    if name == "every":
        return all(truthy(_fn(a[0])(e, i, l)) for i, e in enumerate(l))
    if name == "reduce":
        f = _fn(a[0])
        it = list(enumerate(l))
        if len(a) > 1:
            acc = a[1]
        elif it:
            acc, it = it[0][1], it[1:]
        else:
            raise JSTypeError("reduce of empty array with no initial value")
        for i, e in it:
            acc = f(acc, e, i, l)
        return acc
    if name == "splice":
        s = _slice_idx(a[0], len(l), 0)
        n = len(l) - s if len(a) < 2 else max(0, int(num(a[1])))
        removed = l[s:s + n]
        l[s:s + n] = a[2:]
        return removed
    if name == "toString":
        return tostr(l)
    # java.util.List methods: in Data Collector a list field is a Java ArrayList, so scripts call these
    if name == "add":
        if len(a) == 2:
            l.insert(int(num(a[0])), a[1])
            return UNDEF
        l.append(a[0])
        return True
    if name == "size":
        return len(l)
    if name == "get":
        return l[int(num(a[0]))] if 0 <= num(a[0]) < len(l) else UNDEF
    if name == "isEmpty":
        return len(l) == 0
    if name == "contains":
        return any(seq(e, a[0]) for e in l)
    if name == "remove":
        return l.pop(int(num(a[0])))
    raise JSTypeError(f"{name} is not a function")


def call(o, name, args):
    """`o.name(args)`."""
    if o is None or o is UNDEF:
        raise JSTypeError(f"cannot read property '{name}' of {'null' if o is None else 'undefined'}")
    if isinstance(o, str):
        return _string_method(o, name, args)
    if isinstance(o, list):
        return _array_method(o, name, args)
    if isinstance(o, JSRegex):
        if name == "test":
            return o.test(args[0])
        raise JSTypeError(f"{name} is not a function")
    if isinstance(o, dict):
        if name == "hasOwnProperty":
            return tostr(args[0]) in o
        # java.util.Map methods: in Data Collector a map field is a Java LinkedHashMap
        if name == "put":
            old = o.get(tostr(args[0]), None)
            o[tostr(args[0])] = args[1]
            return old
        if name == "get" and name not in o:
            return o.get(tostr(args[0]), None)
        if name == "containsKey":
            return tostr(args[0]) in o
        if name == "size":
            return len(o)
        if name == "isEmpty":
            return len(o) == 0
        if name == "keySet":
            return list(o.keys())
        if name == "values":
            return list(o.values())
        if name == "remove" and name not in o:
            return o.pop(tostr(args[0]), None)
        if name == "toString":
            return tostr(o)
        f = o.get(name)
        if callable(f):
            return f(*args)
        raise JSTypeError(f"{name} is not a function")
    if isinstance(o, (int, float)) and not isinstance(o, bool):
        if name == "toFixed":
            return f"{o:.{int(num(args[0])) if args else 0}f}"
        if name == "toString":
            return tostr(o)
    if isinstance(o, bool) and name == "toString":
        return tostr(o)
    f = getattr(o, name, None)
    if callable(f):
        return f(*args)
    raise JSTypeError(f"{name} is not a function")


def new_array(*a):
    return [UNDEF] * int(num(a[0])) if len(a) == 1 and isinstance(a[0], (int, float)) else list(a)


# ---- globals -------------------------------------------------------------------------------------------------------------------------
class _Math:
    PI, E = math.pi, math.e
    floor = staticmethod(lambda x: _intify(math.floor(num(x))) if _fin(num(x)) else num(x))
    ceil = staticmethod(lambda x: _intify(math.ceil(num(x))) if _fin(num(x)) else num(x))
    round = staticmethod(lambda x: _intify(math.floor(num(x) + 0.5)) if _fin(num(x)) else num(x))
    abs = staticmethod(lambda x: abs(num(x)))
    sqrt = staticmethod(lambda x: math.sqrt(num(x)) if num(x) >= 0 else float("nan"))
    pow = staticmethod(lambda a, b: _intify(num(a) ** num(b)) if _fin(num(a)) else float("nan"))
    max = staticmethod(lambda *a: float("-inf") if not a else (float("nan") if any(num(x) != num(x) for x in a) else max(num(x) for x in a)))
    min = staticmethod(lambda *a: float("inf") if not a else (float("nan") if any(num(x) != num(x) for x in a) else min(num(x) for x in a)))
    trunc = staticmethod(lambda x: int(num(x)))


def _fin(x):
    return not (isinstance(x, float) and (math.isnan(x) or math.isinf(x)))


def _intify(x):
    return int(x) if isinstance(x, float) and _fin(x) and x == int(x) and abs(x) < 1e15 else x


def _parse_float(s):
    m = re.match(r"\s*[+-]?(\d+\.?\d*([eE][+-]?\d+)?|\.\d+([eE][+-]?\d+)?|Infinity)", tostr(s))
    if not m:
        return float("nan")
    v = float(m.group(0).strip().replace("Infinity", "inf"))
    return _intify(v)


def _parse_int(s, radix=UNDEF):
    r = 10 if radix is UNDEF or num(radix) == 0 else int(num(radix))
    m = re.match(r"\s*([+-]?)(0[xX])?([0-9a-zA-Z]+)", tostr(s))
    if not m:
        return float("nan")
    digits = m.group(3)
    if m.group(2) and r in (10, 16):
        r = 16
    good = ""
    for ch in digits:
        try:
            int(ch, r)
            good += ch
        except ValueError:
            break
    if not good:
        return float("nan")
    return -int(good, r) if m.group(1) == "-" else int(good, r)


def _json_default(o):
    return tostr(o)


def normalize(v):
    """What a value becomes when it crosses back through JSON: undefined properties vanish, NaN and Infinity become null, integral floats become integers."""
    if isinstance(v, dict):
        return {k: normalize(x) for k, x in v.items() if x is not UNDEF}
    if isinstance(v, list):
        return [None if x is UNDEF else normalize(x) for x in v]
    if isinstance(v, float):
        return None if (math.isnan(v) or math.isinf(v)) else (int(v) if v == int(v) and abs(v) < 1e15 else v)
    if v is UNDEF:
        return None
    return v


class _JSON:
    parse = staticmethod(lambda s: _json_parse(s))
    stringify = staticmethod(lambda v, *a: UNDEF if v is UNDEF else json.dumps(normalize(v), separators=(",", ":"), default=_json_default))


def _json_parse(s):
    try:
        return json.loads(tostr(s))
    except ValueError as e:
        raise JSError(ErrorObj("SyntaxError", str(e)))


def _must_object(o):
    if o is None or o is UNDEF:
        raise JSTypeError("cannot convert undefined or null to object")
    return True


class J:
    """Namespace the transpiled code refers to."""
    NaN, Infinity = float("nan"), float("inf")
    Math, JSON = _Math, _JSON
    parseFloat = staticmethod(_parse_float)
    parseInt = staticmethod(_parse_int)
    String = staticmethod(lambda x=UNDEF: "" if x is UNDEF and False else tostr(x))
    Number = staticmethod(lambda x=0: num(x))
    Boolean = staticmethod(lambda x=UNDEF: truthy(x))
    isNaN = staticmethod(lambda x: (lambda n: isinstance(n, float) and n != n)(num(x)))
    Error = staticmethod(lambda msg="": ErrorObj("Error", tostr(msg)))
    TypeError = staticmethod(lambda msg="": ErrorObj("TypeError", tostr(msg)))
    RegExp = staticmethod(lambda p, f="": JSRegex(tostr(p), tostr(f) if f is not UNDEF else ""))
    Array = staticmethod(new_array)
    Object = staticmethod(lambda *a: {})
    keys_of = staticmethod(keys)
    add, sub, mul, div, mod, neg = map(staticmethod, (add, sub, mul, div, mod, neg))
    eq, seq, lt, gt, le, ge = map(staticmethod, (eq, seq, lt, gt, le, ge))
    truthy, typeof, tostr, num, has, instanceof = map(staticmethod, (truthy, typeof, tostr, num, has, instanceof))
    get, put, delete, call = map(staticmethod, (get, put, delete, call))
    regex = staticmethod(lambda p, f="": JSRegex(p, f))
    errval = staticmethod(lambda e: e.value if isinstance(e, JSError) else ErrorObj("Error", str(e)))
    object_keys = staticmethod(lambda o: _must_object(o) and keys(o))
    object_values = staticmethod(lambda o: _must_object(o) and (list(o.values()) if isinstance(o, dict) else (list(o) if isinstance(o, list) else [])))
