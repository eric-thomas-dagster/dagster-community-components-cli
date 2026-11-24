"""Translate ADF's expression language to Python source (strings that call into runtime.py's `fx` and the `ctx` object).

An ADF value is either a literal or {"value": "...", "type": "Expression"}. Inside an expression, "@expr" is the whole value and "@{expr}" is
interpolation into text; "@@" is a literal "@". Anything outside the supported list raises Untranslatable and the activity becomes a loud stub:
we never guess what an unknown function does.
"""
from __future__ import annotations

import re


class Untranslatable(Exception):
    pass


# ADF function (lower case) -> attribute on runtime.fx
FUNCS = {
    "concat": "concat", "equals": "equals", "not": "not_", "and": "and_", "or": "or_", "if": "if_", "string": "string", "int": "int_",
    "float": "float_", "bool": "bool_", "contains": "contains", "replace": "replace", "split": "split", "join": "join", "length": "length",
    "empty": "empty", "first": "first", "last": "last", "add": "add", "sub": "sub", "mul": "mul", "div": "div", "mod": "mod",
    "greater": "greater", "greaterorequals": "greaterorequals", "less": "less", "lessorequals": "lessorequals",
    "formatdatetime": "formatdatetime", "utcnow": "utcnow", "adddays": "adddays", "addhours": "addhours", "addminutes": "addminutes",
    "addseconds": "addseconds", "startswith": "startswith", "endswith": "endswith", "tolower": "tolower", "toupper": "toupper",
    "trim": "trim", "substring": "substring", "indexof": "indexof", "coalesce": "coalesce", "json": "json_", "createarray": "createarray",
    "union": "union", "take": "take", "skip": "skip", "base64": "base64", "base64tostring": "base64tostring",
    "uricomponent": "uricomponent", "guid": "guid", "min": "min_", "max": "max_", "range": "range_", "startofday": "startofday",
    "startofhour": "startofhour", "startofmonth": "startofmonth", "subtractfromtime": "subtractfromtime", "addtotime": "addtotime",
    "dayofmonth": "dayofmonth", "dayofweek": "dayofweek", "dayofyear": "dayofyear", "array": "array", "binary": "binary",
    "lastindexof": "lastindexof", "decodebase64": "base64tostring", "decodeuricomponent": "uricomponenttostring", "encodeuricomponent": "uricomponent",
    "uricomponenttostring": "uricomponenttostring", "datauri": "datauri", "datauritostring": "datauritostring", "getfuturetime": "getfuturetime",
    "getpasttime": "getpasttime", "convertfromutc": "convertfromutc", "converttoutc": "converttoutc", "converttimezone": "converttimezone",
    "intersection": "intersection", "rand": "rand", "ticks": "ticks",
}

_SYS = {"pipeline", "runid", "datafactory", "triggertype", "triggerid", "triggername", "triggertime", "groupid", "triggeredbypipelinename", "triggeredbypipelinerunid"}
_TRIGGER = {"scheduledtime", "starttime", "windowstarttime", "windowendtime", "name"}


class _P:
    def __init__(self, s: str, i: int = 0, ds: str = ""):
        self.s, self.i, self.ds = s, i, ds

    def ws(self):
        while self.i < len(self.s) and self.s[self.i].isspace():
            self.i += 1

    def peek(self) -> str:
        self.ws()
        return self.s[self.i] if self.i < len(self.s) else ""

    def eat(self, ch: str) -> bool:
        if self.peek() == ch:
            self.i += 1
            return True
        return False

    def need(self, ch: str):
        if not self.eat(ch):
            raise Untranslatable(f"expected {ch!r} at position {self.i} of {self.s!r}")

    def ident(self) -> str:
        self.ws()
        m = re.compile(r"[A-Za-z_][A-Za-z0-9_]*").match(self.s, self.i)
        if not m:
            raise Untranslatable(f"expected a name at position {self.i} of {self.s!r}")
        self.i = m.end()
        return m.group(0)

    def string(self) -> str:
        self.need("'")
        out = []
        while True:
            if self.i >= len(self.s):
                raise Untranslatable(f"unterminated string in {self.s!r}")
            c = self.s[self.i]
            if c == "'":
                if self.s[self.i + 1:self.i + 2] == "'":
                    out.append("'")
                    self.i += 2
                    continue
                self.i += 1
                return "".join(out)
            out.append(c)
            self.i += 1

    def expr(self) -> str:
        c = self.peek()
        if c == "'":
            base, acc = ("str", self.string()), []
        elif c.isdigit() or c == "-" or (c == "." and self.s[self.i + 1:self.i + 2].isdigit()):
            m = re.compile(r"-?(\d+(\.\d+)?|\.\d+)").match(self.s, self.i)
            if not m:
                raise Untranslatable(f"bad number in {self.s!r}")
            self.i = m.end()
            base, acc = ("num", m.group(0)), []
        elif c.isalpha() or c == "_":
            name = self.ident()
            if self.peek() == "(":
                self.i += 1
                args = []
                if not self.eat(")"):
                    while True:
                        args.append(self.expr())
                        if self.eat(")"):
                            break
                        self.need(",")
                base, acc = ("call", name, args), []
            elif name.lower() in ("true", "false", "null"):
                base, acc = ("lit", {"true": "True", "false": "False", "null": "None"}[name.lower()]), []
            else:
                raise Untranslatable(f"bare name {name!r} in {self.s!r}")
        else:
            raise Untranslatable(f"unexpected {c!r} at position {self.i} of {self.s!r}")
        acc = []
        while True:
            if self.s.startswith("?.", self.i):
                self.i += 2
                acc.append((".", self.ident(), True))
            elif self.peek() == ".":
                self.i += 1
                acc.append((".", self.ident(), False))
            elif self.peek() == "[":
                self.i += 1
                e = self.expr()
                self.need("]")
                acc.append(("[", e, False))
            else:
                break
        return self.compile(base, acc)

    # --- compile to python source -------------------------------------------------------------------------------------------------------------------
    def compile(self, base, acc) -> str:
        kind = base[0]
        if kind == "str":
            return repr(base[1])
        if kind == "num":
            return base[1]
        if kind == "lit":
            return base[1]
        _, name, args = base
        low = name.lower()
        acc = list(acc)

        def lit_arg() -> str:
            if len(args) != 1 or not args[0].startswith(("'", '"')):
                raise Untranslatable(f"{name}() needs a literal name in {self.s!r}")
            return args[0]

        if low == "pipeline":
            if not acc or acc[0][0] != ".":
                raise Untranslatable(f"pipeline() without a property in {self.s!r}")
            prop = acc.pop(0)[1]
            if prop.lower() == "parameters":
                if not acc:
                    raise Untranslatable("pipeline().parameters used as a whole")
                kind2, val, _ = acc.pop(0)
                code = f"ctx.param({repr(val) if kind2 == '.' else val})"
            elif prop.lower() == "globalparameters":
                if not acc or acc[0][0] != ".":
                    raise Untranslatable("pipeline().globalParameters used as a whole")
                code = f"ctx.global_param({acc.pop(0)[1]!r})"
            elif prop.lower() in _SYS:
                code = f"ctx.sys({prop.lower()!r})"
            else:
                raise Untranslatable(f"pipeline().{prop} is not supported")
        elif low == "activity":
            n = lit_arg()
            if not acc or acc[0][0] != ".":
                raise Untranslatable(f"activity({n}) without .output, .error or .status")
            prop = acc.pop(0)[1].lower()
            if prop not in ("output", "error", "status", "statuscode"):
                raise Untranslatable(f"activity({n}).{prop} is not supported")
            code = f"ctx.{ {'output': 'out', 'error': 'err', 'status': 'status', 'statuscode': 'status_code'}[prop]}({n})"
        elif low == "variables":
            code = f"ctx.var({lit_arg()})"
        elif low == "item":
            code = "item"
        elif low == "trigger":
            if acc and acc[0][0] == "." and acc[0][1].lower() == "outputs":
                acc.pop(0)
            if not acc or acc[0][0] != "." or acc[0][1].lower() not in _TRIGGER:
                raise Untranslatable(f"trigger() property not supported in {self.s!r}")
            code = f"ctx.trigger({acc.pop(0)[1]!r})"
        elif low == "dataset":
            if not self.ds or not acc or acc[0][0] != ".":
                raise Untranslatable("dataset() outside a dataset")
            code = f"fx.get({self.ds}, {acc.pop(0)[1]!r})"
        elif low in FUNCS:
            code = f"fx.{FUNCS[low]}({', '.join(args)})"
        else:
            raise Untranslatable(f"function {name}() is not supported")
        for kind2, val, opt in acc:
            key = repr(val) if kind2 == "." else val
            code = f"fx.get({code}, {key}{', opt=True' if opt else ''})"
        return code


def translate_text(s: str, ds: str = "") -> str:
    """An Expression-typed string: whole-value "@expr", or text with "@{expr}" pieces and "@@" escapes."""
    if s.startswith("@") and not s.startswith(("@{", "@@")):
        p = _P(s, 1, ds)
        code = p.expr()
        p.ws()
        if p.i != len(s):
            raise Untranslatable(f"trailing text after the expression in {s!r}")
        return code
    parts, i, lit = [], 0, []
    while i < len(s):
        if s.startswith("@@", i):
            lit.append("@")
            i += 2
        elif s.startswith("@{", i):
            if lit:
                parts.append(repr("".join(lit)))
                lit = []
            p = _P(s, i + 2, ds)
            parts.append(p.expr())
            p.need("}")
            i = p.i
        else:
            lit.append(s[i])
            i += 1
    if lit:
        parts.append(repr("".join(lit)))
    if not parts:
        return "''"
    if len(parts) == 1 and not s.startswith("@{"):
        return parts[0]
    return f"fx.interp([{', '.join(parts)}])"


def _expr_string(s: str) -> bool:
    """ADF evaluates any string that starts with @ or holds @{...} as an expression, with or without the {"type": "Expression"} wrapper
    (a leading @@ is the escape for a literal @)."""
    return isinstance(s, str) and (s.startswith("@") or "@{" in s)


def translate(value, ds: str = "") -> str:
    """Python source for an ADF value (a literal, an Expression object, or a list or dict of those)."""
    if isinstance(value, dict):
        if value.get("type") == "Expression" and "value" in value:
            return translate_text(str(value["value"]), ds)
        return "{" + ", ".join(f"{k!r}: {translate(v, ds)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(translate(v, ds) for v in value) + "]"
    if isinstance(value, str):
        if re.match(r"^\[(parameters|concat|variables)\(", value):
            raise Untranslatable(f"deploy-time value {value} was not resolved")
        if _expr_string(value):
            return translate_text(value, ds)
    return repr(value)


def is_dynamic(value) -> bool:
    """True when the value contains an expression."""
    if isinstance(value, str):
        return _expr_string(value)
    if isinstance(value, dict):
        return value.get("type") == "Expression" or any(is_dynamic(v) for v in value.values())
    if isinstance(value, list):
        return any(is_dynamic(v) for v in value)
    return False
