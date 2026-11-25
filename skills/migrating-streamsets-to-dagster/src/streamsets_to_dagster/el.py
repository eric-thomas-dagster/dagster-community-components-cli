"""Translate the StreamSets expression language (EL, JSP-style `${...}`) to Python source.

Output calls into the generated project's runtime (`fx.*`). Variables available to translated code: `rec` (the record), `C` (runtime
parameters, i.e. the pipeline's constants), `f` (field context inside Field Mapper). Anything outside the supported list raises
Untranslatable and the stage becomes a loud stub: an unknown function is never guessed at.
"""
from __future__ import annotations

import re


class Untranslatable(Exception):
    pass


# namespace:function -> runtime.fx attribute
FUNCS = {
    "record:value": "record_value", "record:valueOrDefault": "record_value_or", "record:attribute": "record_attribute",
    "record:attributeOrDefault": "record_attribute_or", "record:exists": "record_exists", "record:eventType": "record_event_type",
    "str:toLower": "to_lower", "str:toUpper": "to_upper", "str:trim": "trim", "str:length": "length", "str:substring": "substring",
    "str:concat": "concat", "str:replace": "replace", "str:replaceAll": "replace_all", "str:contains": "contains",
    "str:startsWith": "starts_with", "str:endsWith": "ends_with", "str:matches": "matches", "str:regExCapture": "reg_ex_capture",
    "str:indexOf": "index_of", "str:truncate": "truncate", "str:isNullOrEmpty": "is_null_or_empty",
    "math:abs": "m_abs", "math:ceil": "m_ceil", "math:floor": "m_floor", "math:round": "m_round", "math:max": "m_max", "math:min": "m_min",
    "time:now": "time_now", "file:fileName": "file_name", "file:parentPath": "file_parent_path", "file:pathElement": "file_path_element",
    "file:removeExtension": "file_remove_extension", "f:name": "f_name", "f:value": "f_value", "f:path": "f_path", "f:type": "f_type",
    "f:index": "f_index", "f:parentPath": "f_parent_path", "uuid": "uuid", "YYYY": "yyyy", "MM": "mm", "DD": "dd", "hh": "hh",
    "mm": "minute", "ss": "ss", "emptyMap": "empty_map", "emptyList": "empty_list",
    "time:dateTimeToMilliseconds": "date_time_to_ms", "time:createDateFromStringTZ": "create_date_from_string_tz",
    "time:createDateFromString": "create_date_from_string", "time:extractStringFromDate": "extract_string_from_date",
    "time:millisecondsToDateTime": "ms_to_date_time", "time:dateTimeZoneOffset": "date_time_zone_offset", "pipeline:id": "pipeline_id", "pipeline:title": "pipeline_title", "pipeline:name": "pipeline_id",
}
_TIME_UNITS = {"SECONDS": 1, "MINUTES": 60, "HOURS": 3600, "DAYS": 86400}
_KEYWORDS = {"true": "True", "false": "False", "null": "None"}
_WORD_OPS = {"eq": "==", "ne": "!=", "lt": "<", "gt": ">", "le": "<=", "ge": ">=", "and": "&&", "or": "||", "not": "!", "div": "/", "mod": "%",
             "empty": "empty"}

_TOKEN = re.compile(r"""\s*(?:
    (?P<num>\d+(?:\.\d+)?)|
    (?P<str>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")|
    (?P<id>[A-Za-z_][A-Za-z0-9_]*(?::[A-Za-z_][A-Za-z0-9_]*)?)|
    (?P<op>\|\||&&|==|!=|<=|>=|[-+*/%<>!?:(),.\[\]])
)""", re.X)


def _tokens(s: str):
    i, out = 0, []
    while i < len(s):
        if s[i:].strip() == "":
            break
        m = _TOKEN.match(s, i)
        if not m:
            raise Untranslatable(f"cannot read {s[i:i + 20]!r} in expression {s!r}")
        i = m.end()
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
    return out


class _P:
    def __init__(self, src: str, toks):
        self.src, self.t, self.i = src, toks, 0

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def take(self):
        tok = self.peek()
        self.i += 1
        return tok

    def op(self, *ops) -> str | None:
        k, v = self.peek()
        if k == "op" and v in ops:
            self.i += 1
            return v
        if k == "id" and v in _WORD_OPS and _WORD_OPS[v] in ops:
            self.i += 1
            return _WORD_OPS[v]
        return None

    def need(self, o: str):
        if not self.op(o):
            raise Untranslatable(f"expected {o!r} in {self.src!r}")

    # precedence climbing: ternary < || < && < equality < relational < additive < multiplicative < unary
    def ternary(self) -> str:
        c = self.or_()
        if self.op("?"):
            a = self.ternary()
            self.need(":")
            b = self.ternary()
            return f"({a} if {c} else {b})"
        return c

    def or_(self) -> str:
        l = self.and_()
        while self.op("||"):
            l = f"({l} or {self.and_()})"
        return l

    def and_(self) -> str:
        l = self.eq()
        while self.op("&&"):
            l = f"({l} and {self.eq()})"
        return l

    def eq(self) -> str:
        l = self.rel()
        while (o := self.op("==", "!=")):
            r = self.rel()
            l = f"fx.eq({l}, {r})" if o == "==" else f"(not fx.eq({l}, {r}))"
        return l

    def rel(self) -> str:
        l = self.add()
        while (o := self.op("<", ">", "<=", ">=")):
            l = f"fx.cmp({l!s}, {o!r}, {self.add()})"
        return l

    def add(self) -> str:
        l = self.mul()
        while (o := self.op("+", "-")):
            l = f"fx.arith({l}, {o!r}, {self.mul()})"
        return l

    def mul(self) -> str:
        l = self.unary()
        while (o := self.op("*", "/", "%")):
            l = f"fx.arith({l}, {o!r}, {self.unary()})"
        return l

    def unary(self) -> str:
        if self.op("!"):
            return f"(not {self.unary()})"
        if self.op("-"):
            return f"fx.arith(0, '-', {self.unary()})"
        k, v = self.peek()
        if k == "id" and v == "empty":
            self.i += 1
            return f"fx.empty({self.unary()})"
        return self.primary()

    def primary(self) -> str:
        k, v = self.take()
        if k == "num":
            return v
        if k == "str":
            body = v[1:-1]
            return repr(re.sub(r"\\(.)", r"\1", body))
        if k == "op" and v == "(":
            e = self.ternary()
            self.need(")")
            return f"({e})"
        if k == "id":
            if self.peek() == ("op", "("):
                self.i += 1
                args = []
                if not self.op(")"):
                    while True:
                        args.append(self.ternary())
                        if self.op(")"):
                            break
                        self.need(",")
                if v not in FUNCS:
                    raise Untranslatable(f"function {v}() is not supported")
                return f"fx.{FUNCS[v]}({', '.join(['rec', 'f'] if v.startswith('f:') else ['rec'] if v.startswith('record:') else [])}{', ' if args and (v.startswith(('f:', 'record:'))) else ''}{', '.join(args)})"
            if v in _KEYWORDS:
                return _KEYWORDS[v]
            if v in _TIME_UNITS:
                return str(_TIME_UNITS[v])
            if v == "fields":
                return "f_fields"          # Field Mapper aggregation: the mapped fields
            if ":" in v:
                raise Untranslatable(f"{v} is not supported")
            return f"C[{v!r}]"
        raise Untranslatable(f"unexpected {v!r} in {self.src!r}")


def translate_expr(s: str) -> str:
    """Python source for one EL expression body (what is between `${` and `}`)."""
    p = _P(s, _tokens(s))
    code = p.ternary()
    if p.i != len(p.t):
        raise Untranslatable(f"unexpected {p.peek()[1]!r} in {s!r}")
    return code


def _split(text: str):
    """Yield ('lit', text) and ('el', body) pieces of a string with ${...} sections (braces and quotes balanced)."""
    i, lit = 0, []
    while i < len(text):
        if text.startswith("${", i):
            if lit:
                yield "lit", "".join(lit)
                lit = []
            depth, j, q = 1, i + 2, ""
            while j < len(text) and depth:
                c = text[j]
                if q:
                    if c == "\\":
                        j += 1
                    elif c == q:
                        q = ""
                elif c in "'\"":
                    q = c
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                j += 1
            if depth:
                raise Untranslatable(f"unterminated ${{ in {text!r}")
            yield "el", text[i + 2:j - 1]
            i = j
        else:
            lit.append(text[i])
            i += 1
    if lit:
        yield "lit", "".join(lit)


def is_dynamic(v) -> bool:
    return isinstance(v, str) and "${" in v


def translate(v) -> str:
    """Python source for a config value: a literal stays a literal; a string with ${...} becomes an expression."""
    if not isinstance(v, str) or "${" not in v:
        return repr(v)
    parts = list(_split(v))
    if len(parts) == 1 and parts[0][0] == "el":
        return translate_expr(parts[0][1])
    return "fx.interp([" + ", ".join(repr(t) if k == "lit" else translate_expr(t) for k, t in parts) + "])"
